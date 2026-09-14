from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import httpx
import pytest

from osint_harness.domain.provenance import Document
from osint_harness.sources.cassette import (
    Cassette,
    CassetteMissError,
    CassetteMode,
    RecordedCall,
)
from osint_harness.sources.tools import (
    Encyclopedia,
    EncyclopediaResponse,
    PageFetch,
    PlainText,
    Tool,
    WebSearch,
)

FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _document(url: str = "https://example.com/a", text: str = "body") -> Document:
    return Document.retrieved(url=url, title="t", text=text, retrieved_at=FIXED_TIME)


class CountingTool(Tool):
    """A tool that records how often it actually reached its source."""

    def __init__(self, cassette: Cassette) -> None:
        super().__init__(cassette)
        self.live_calls = 0

    @property
    def name(self) -> str:
        return "counting"

    def retrieve(self, query: str) -> tuple[Document, ...]:
        self.live_calls += 1
        return (_document(text=f"fetched for {query}"),)


class FakeResponse:
    """Stands in for an httpx response so tests never reach the network."""

    def __init__(
        self,
        payload: object = None,
        text: str = "",
        url: str = "https://example.com",
    ) -> None:
        self._payload = payload
        self.text = text
        self.url = url
        self.headers: dict[str, str] = {}

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> Self:
        return self


class TestCassetteKeys:
    def test_key_is_stable_for_the_same_lookup(self) -> None:
        assert Cassette.key_for("web", "Acme Corp") == Cassette.key_for("web", "Acme Corp")

    def test_key_ignores_incidental_whitespace(self) -> None:
        assert Cassette.key_for("web", "Acme  Corp\n") == Cassette.key_for("web", "Acme Corp")

    def test_different_tools_asking_the_same_question_do_not_collide(self) -> None:
        assert Cassette.key_for("web", "Acme") != Cassette.key_for("encyclopedia", "Acme")


class TestCassetteStorage:
    def test_round_trip_preserves_documents_exactly(self, tmp_path: Path) -> None:
        cassette = Cassette(mode=CassetteMode.RECORD)
        key = Cassette.key_for("web", "Acme")
        cassette.record(key, RecordedCall(tool="web", query="Acme", documents=(_document(),)))
        path = tmp_path / "nested" / "cassette.json"
        cassette.write_to(path)

        reloaded = Cassette.load(path)
        assert reloaded.replay(key) == (_document(),)
        assert reloaded.replay(key)[0].retrieved_at == FIXED_TIME

    def test_loading_a_missing_file_starts_empty_rather_than_failing(self, tmp_path: Path) -> None:
        cassette = Cassette.load(tmp_path / "absent.json")
        assert cassette.calls == {}

    def test_mode_is_not_written_into_the_recording(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        Cassette(mode=CassetteMode.RECORD).write_to(path)
        assert "record" not in path.read_text(encoding="utf-8")

    def test_replaying_an_unrecorded_key_is_an_error_not_an_empty_result(self) -> None:
        with pytest.raises(CassetteMissError):
            Cassette().replay("nope")


class TestToolReplay:
    def test_replay_mode_refuses_to_reach_the_network_on_a_miss(self) -> None:
        tool = CountingTool(Cassette(mode=CassetteMode.REPLAY))
        with pytest.raises(CassetteMissError, match="counting"):
            tool.gather("Acme")
        assert tool.live_calls == 0

    def test_recording_mode_fetches_once_then_replays(self) -> None:
        tool = CountingTool(Cassette(mode=CassetteMode.RECORD))
        first = tool.gather("Acme")
        second = tool.gather("Acme")
        assert first == second
        assert tool.live_calls == 1

    def test_a_recorded_run_replays_without_the_source(self) -> None:
        recording = Cassette(mode=CassetteMode.RECORD)
        CountingTool(recording).gather("Acme")

        replaying = Cassette(calls=recording.calls, mode=CassetteMode.REPLAY)
        tool = CountingTool(replaying)
        assert tool.gather("Acme")[0].text == "fetched for Acme"
        assert tool.live_calls == 0


class TestPlainText:
    def test_drops_script_and_style_content(self) -> None:
        html = "<html><body><script>evil()</script><p>Real text</p><style>p{}</style></body></html>"
        assert PlainText.of(html) == "Real text"

    def test_collapses_whitespace_runs(self) -> None:
        assert PlainText.of("<p>a\n\n   b</p>") == "a b"

    def test_keeps_text_from_unclosed_markup(self) -> None:
        assert "kept" in PlainText.of("<div><p>kept")


class TestEncyclopediaResponse:
    def test_parses_the_mediawiki_shape(self) -> None:
        parsed = EncyclopediaResponse.model_validate(
            {"query": {"pages": {"42": {"pageid": 42, "title": "Acme", "extract": "A company."}}}}
        )
        assert parsed.articles()[0].title == "Acme"

    def test_discards_articles_with_no_text(self) -> None:
        parsed = EncyclopediaResponse.model_validate(
            {
                "query": {
                    "pages": {
                        "1": {"pageid": 1, "title": "Empty", "extract": "   "},
                        "2": {"pageid": 2, "title": "Real", "extract": "Text."},
                    }
                }
            }
        )
        assert [article.title for article in parsed.articles()] == ["Real"]

    def test_an_empty_reply_yields_no_articles(self) -> None:
        assert EncyclopediaResponse.model_validate({}).articles() == ()


class TestEncyclopediaRetrieval:
    def test_builds_an_article_url_from_the_title(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "query": {"pages": {"7": {"pageid": 7, "title": "Acme Corp", "extract": "A firm."}}}
        }
        monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: FakeResponse(payload=payload))
        documents = Encyclopedia(Cassette(mode=CassetteMode.RECORD)).retrieve("Acme")
        assert documents[0].url == "https://en.wikipedia.org/wiki/Acme_Corp"
        assert documents[0].source_domain == "en.wikipedia.org"


TAVILY_SAMPLE = {
    "results": [
        {
            "url": "https://reuters.com/acme",
            "title": "Acme files for court protection",
            "content": "Acme Corp filed for court protection today.",
        },
        {
            "url": "https://ft.com/content/acme-2",
            "title": "Acme raises new funding",
            "content": "Acme Corp raised a new funding round.",
        },
    ]
}


class TestWebSearchRetrieval:
    def _searching(self, monkeypatch: pytest.MonkeyPatch, payload: object) -> WebSearch:
        monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: FakeResponse(payload=payload))
        return WebSearch(Cassette(mode=CassetteMode.RECORD), api_key="test-key")

    def test_returns_documents_from_the_parsed_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        documents = self._searching(monkeypatch, TAVILY_SAMPLE).retrieve("Acme Corp")

        assert documents[0].url == "https://reuters.com/acme"
        assert documents[0].title == "Acme files for court protection"
        assert documents[0].source_domain == "reuters.com"

    def test_the_document_text_is_the_snippet_not_a_full_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        documents = self._searching(monkeypatch, TAVILY_SAMPLE).retrieve("Acme Corp")

        assert documents[0].text == "Acme Corp filed for court protection today."

    def test_results_are_capped_at_max_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        searching = self._searching(monkeypatch, TAVILY_SAMPLE)
        monkeypatch.setattr(WebSearch, "MAX_RESULTS", 1)

        assert len(searching.retrieve("Acme Corp")) == 1

    def test_a_hit_with_no_url_is_dropped_rather_than_cited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {"results": [{"url": "", "title": "nowhere", "content": "x"}]}

        assert self._searching(monkeypatch, payload).retrieve("Acme Corp") == ()

    def test_a_missing_key_refuses_rather_than_searching_for_nothing(self) -> None:
        """The scraper this replaced failed by succeeding with zero results, so an instance with
        no key configured must refuse out loud instead of looking like a search that found
        nothing."""
        searching = WebSearch(Cassette(mode=CassetteMode.RECORD), api_key="")

        with pytest.raises(httpx.HTTPError, match="TAVILY_API_KEY"):
            searching.retrieve("Acme Corp")

    def test_a_reply_that_is_not_results_is_a_failure_not_an_empty_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bot challenge or a throttle notice must never read as "found nothing". This is the
        exact defect that made the old DuckDuckGo scraper return empty on every single call while
        reporting success: HTTP 202 with a challenge page is a 2xx, so nothing raised."""
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *_args, **_kwargs: FakeResponse(text="<html>please verify you are human</html>"),
        )
        searching = WebSearch(Cassette(mode=CassetteMode.RECORD), api_key="test-key")

        with pytest.raises(httpx.HTTPError, match="did not return results"):
            searching.retrieve("Acme Corp")


class TestPageFetchRetrieval:
    def test_extracts_readable_text_and_keeps_the_final_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            httpx,
            "get",
            lambda *_args, **_kwargs: FakeResponse(
                text="<html><body><p>Acme filed accounts.</p></body></html>",
                url="https://reuters.com/article/1",
            ),
        )
        documents = PageFetch(Cassette(mode=CassetteMode.RECORD)).retrieve("https://reuters.com/x")
        assert documents[0].text == "Acme filed accounts."
        assert documents[0].source_domain == "reuters.com"


class TypedResponse(FakeResponse):
    """A fake response that also declares what kind of content it carries."""

    def __init__(self, text: str, content_type: str) -> None:
        super().__init__(text=text, url="https://example.com/file")
        self.headers = {"content-type": content_type}


class TestUnreadablePages:
    """Found live: the fetcher read PDFs as if they were HTML. Three documents in one Wirecard
    investigation were PDFs stripped of "tags" into hundreds of thousands of characters, about half
    of them binary, all recorded as evidence; in a later run a byte sequence starting `<![` crashed
    the HTML parser and took the whole investigation down with it."""

    def test_an_unrecognised_marked_section_is_read_as_text_rather_than_crashing(self) -> None:
        text = PlainText.of("<p>kept</p><![if !IE]><p>also kept</p><![8}~")

        assert "kept" in text
        assert "also kept" in text

    def test_a_pdf_is_refused_as_a_failed_lookup_rather_than_read_as_a_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            httpx, "get", lambda *_args, **_kwargs: TypedResponse("%PDF-1.7 ...", "application/pdf")
        )

        with pytest.raises(httpx.HTTPError, match="application/pdf"):
            PageFetch(Cassette(mode=CassetteMode.RECORD)).retrieve("https://example.com/file.pdf")

    def test_a_declared_html_page_with_a_charset_is_still_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = TypedResponse("<p>Acme filed.</p>", "text/html; charset=utf-8")
        monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: page)

        documents = PageFetch(Cassette(mode=CassetteMode.RECORD)).retrieve("https://example.com/a")

        assert documents[0].text == "Acme filed."
