from abc import ABC, abstractmethod
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from osint_harness.domain.provenance import Document
from osint_harness.sources.cassette import Cassette, CassetteMissError, CassetteMode, RecordedCall


class Tool(ABC):
    """An external source the investigation can query, recorded so the run can be replayed."""

    USER_AGENT: ClassVar[str] = (
        "agentic-osint-harness/0.1 "
        "(https://github.com/Kritantasasanroy/agentic-osint-harness) python-httpx"
    )

    def __init__(self, cassette: Cassette, timeout_seconds: float = 20.0) -> None:
        self._cassette = cassette
        self._timeout_seconds = timeout_seconds

    @property
    @abstractmethod
    def name(self) -> str:
        """The identifier this tool is recorded and reported under."""

    @abstractmethod
    def retrieve(self, query: str) -> tuple[Document, ...]:
        """Reach the external source for real. Only ever called while recording."""

    def gather(self, query: str) -> tuple[Document, ...]:
        """Documents for this query, replayed if recorded and fetched live only when recording."""
        key = Cassette.key_for(self.name, query)
        if self._cassette.holds(key):
            return self._cassette.replay(key)
        if self._cassette.mode is CassetteMode.REPLAY:
            raise CassetteMissError(
                f"{self.name} was asked for {query!r}, which is not in the cassette; "
                "record it first or run with --record"
            )
        documents = self.retrieve(query)
        self._cassette.record(
            key, RecordedCall(tool=self.name, query=query, documents=documents)
        )
        return documents


class PlainText(HTMLParser):
    """An HTML document reduced to its readable text, with script and style content dropped."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._muted = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._muted += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._muted > 0:
            self._muted -= 1

    def handle_data(self, data: str) -> None:
        if self._muted == 0:
            self._chunks.append(data)

    def text(self) -> str:
        """The extracted text, with runs of whitespace collapsed."""
        return " ".join("".join(self._chunks).split())

    @classmethod
    def of(cls, html: str) -> str:
        """Read an HTML document and return its readable text."""
        reader = cls()
        reader.feed(html)
        return reader.text()


class EncyclopediaPage(BaseModel):
    """One article in an encyclopedia search response."""

    pageid: int
    title: str
    extract: str = ""


class EncyclopediaPages(BaseModel):
    """The article set a search returned, keyed by the encyclopedia's own page identifiers."""

    pages: dict[str, EncyclopediaPage] = Field(default_factory=dict)


class EncyclopediaResponse(BaseModel):
    """The MediaWiki API's reply to a search, typed at the boundary rather than read as a dict."""

    query: EncyclopediaPages = Field(default_factory=EncyclopediaPages)

    def articles(self) -> tuple[EncyclopediaPage, ...]:
        """The returned articles that actually carry text."""
        return tuple(page for page in self.query.pages.values() if page.extract.strip())


class Encyclopedia(Tool):
    """Wikipedia, queried for background and for disambiguating entities that share a name."""

    ENDPOINT = "https://en.wikipedia.org/w/api.php"

    @property
    def name(self) -> str:
        return "encyclopedia"

    def retrieve(self, query: str) -> tuple[Document, ...]:
        response = httpx.get(
            self.ENDPOINT,
            params={
                "action": "query",
                "format": "json",
                "prop": "extracts",
                "exintro": "1",
                "explaintext": "1",
                "redirects": "1",
                "generator": "search",
                "gsrsearch": query,
                "gsrlimit": "3",
            },
            headers={"User-Agent": self.USER_AGENT},
            timeout=self._timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
        parsed = EncyclopediaResponse.model_validate(response.json())
        return tuple(
            Document.retrieved(
                url=f"https://en.wikipedia.org/wiki/{quote(article.title.replace(' ', '_'))}",
                title=article.title,
                text=article.extract,
            )
            for article in parsed.articles()
        )


class PageFetch(Tool):
    """A specific web page, pulled so a cited claim can be read at its own source."""

    @property
    def name(self) -> str:
        return "page_fetch"

    def retrieve(self, query: str) -> tuple[Document, ...]:
        response = httpx.get(
            query,
            headers={"User-Agent": self.USER_AGENT},
            timeout=self._timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
        # ponytail: stdlib tag-strip, swap for trafilatura if extraction quality limits evidence
        text = PlainText.of(response.text)
        return (Document.retrieved(url=str(response.url), title=self._title_of(text), text=text),)

    def _title_of(self, text: str) -> str:
        """A usable title for a page whose markup did not give a clean one."""
        opening = text[:80].strip()
        return opening if opening else "untitled page"
