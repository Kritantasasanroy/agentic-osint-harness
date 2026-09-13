import os
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



class SearchHit(BaseModel):
    """One result from the search provider, typed at the boundary rather than read as a dict."""

    url: str = ""
    title: str = ""
    content: str = ""


class SearchResponse(BaseModel):
    """The provider's reply to one query."""

    results: tuple[SearchHit, ...] = ()

    def usable(self) -> tuple[SearchHit, ...]:
        """The hits that actually give somewhere to go."""
        return tuple(hit for hit in self.results if hit.url.strip())


class WebSearch(Tool):
    """Web search, through Tavily's API.

    This scraped DuckDuckGo's HTML front end until a live run showed that broken in the worst way
    available: DuckDuckGo answers a self-identifying client with HTTP 202 and a bot-challenge page
    rather than results. 202 is a success code, so `raise_for_status` stayed quiet, the parser found
    no results in a page that genuinely had none, and every search came back empty without ever
    being recorded as a failure. Live investigations reached exactly one source, Wikipedia, and
    reported that as a clean run. GDELT's keyless API was measured as a replacement and rejected on
    evidence: 429 on roughly half of all calls even paced ten seconds apart, and 50 to 80 seconds
    per search once retries were counted.

    So this uses a real search API with a key, which is what the `ponytail:` note on the old scraper
    always said the upgrade path was. A key is required rather than optional: a search that silently
    does nothing is worse than one that refuses to start, which is the entire lesson of the scraper
    it replaced.

    Results carry the provider's snippet, never the article body: a hit is a lead to weigh, not
    evidence to cite. `Collection` decides which are worth opening in full through `PageFetch`
    before anything is recorded against the investigation.
    """

    ENDPOINT = "https://api.tavily.com/search"
    MAX_RESULTS: ClassVar[int] = 6

    def __init__(
        self, cassette: Cassette, timeout_seconds: float = 20.0, api_key: str | None = None
    ) -> None:
        super().__init__(cassette, timeout_seconds)
        self._api_key = api_key if api_key is not None else os.environ.get("TAVILY_API_KEY", "")

    @property
    def name(self) -> str:
        return "web_search"

    def retrieve(self, query: str) -> tuple[Document, ...]:
        if not self._api_key:
            raise httpx.HTTPError(
                "TAVILY_API_KEY is not set, so live web search is unavailable on this instance"
            )
        response = httpx.post(
            self.ENDPOINT,
            json={
                "query": query,
                "max_results": self.MAX_RESULTS,
                "search_depth": "basic",
            },
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": self.USER_AGENT,
            },
            timeout=self._timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
        return tuple(
            Document.retrieved(
                url=hit.url,
                title=hit.title or hit.url,
                text=hit.content or hit.title or hit.url,
            )
            for hit in self._parsed(response).usable()[: self.MAX_RESULTS]
        )

    def _parsed(self, response: httpx.Response) -> SearchResponse:
        """The results, refusing a reply that is not actually results.

        A search that legitimately finds nothing returns an empty list, and that is a real answer
        worth recording as one. A challenge page or a throttle notice is not, and telling those
        apart is the whole point: the scraper this replaced could not, so a wholly blocked search
        kept passing as a successful one that happened to find nothing.
        """
        try:
            return SearchResponse.model_validate(response.json())
        except ValueError as failure:
            summary = " ".join(response.text.split())[:160]
            raise httpx.HTTPError(f"search did not return results: {summary}") from failure
