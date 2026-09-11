import anthropic
from anthropic.types import WebSearchTool20260209Param
from pydantic import BaseModel, Field

from osint_harness.model.client import (
    ModelClient,
    ModelRefusedError,
    ModelUnavailableError,
    SearchFindings,
    SearchResult,
    Usage,
)


class WebSearchHit(BaseModel):
    """One result inside a server-side search block."""

    url: str = ""
    title: str = ""


class WebSearchFailure(BaseModel):
    """A failed search. The provider reports these with a 200, so they are read, never caught."""

    error_code: str


class ResponseBlock(BaseModel):
    """One block of a model reply, read only for what a search result would carry."""

    type: str = ""
    content: list[WebSearchHit] | WebSearchFailure = Field(default_factory=list)


class ModelReply(BaseModel):
    """A model reply reduced to the parts this harness reads, typed rather than indexed."""

    content: list[ResponseBlock] = Field(default_factory=list)

    def search_hits(self) -> tuple[WebSearchHit, ...]:
        """Every result across the reply's search blocks, failing loudly if a search errored."""
        hits: list[WebSearchHit] = []
        for block in self.content:
            if block.type != "web_search_tool_result":
                continue
            if isinstance(block.content, WebSearchFailure):
                raise ModelUnavailableError(f"web search failed: {block.content.error_code}")
            hits.extend(block.content)
        return tuple(hits)


class LiveModel(ModelClient):
    """The hosted reasoning engine, reached over the provider's API.

    Refusal fallbacks are deliberately not enabled. Re-running a declined request on a different
    model would mean episodes in the same benchmark were produced by different models, which
    destroys the comparison the harness exists to make. A refusal is raised so the episode is
    tagged as a failure and stays in the denominator.
    """

    def __init__(
        self,
        model: str = "claude-opus-5",
        max_tokens: int = 16000,
        max_search_uses: int = 4,
    ) -> None:
        super().__init__()
        self._client = anthropic.Anthropic()
        self._model = model
        self._max_tokens = max_tokens
        self._max_search_uses = max_search_uses

    def decide[T: BaseModel](self, purpose: str, system: str, prompt: str, schema: type[T]) -> T:
        response = self._client.messages.parse(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        self._charge(
            Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        )
        if response.stop_reason == "refusal":
            raise ModelRefusedError(f"the model declined to answer during {purpose}")
        parsed = response.parsed_output
        if parsed is None:
            raise ModelUnavailableError(f"no structured reply returned during {purpose}")
        return parsed

    def search(self, query: str) -> SearchFindings:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": query}],
            tools=[
                WebSearchTool20260209Param(
                    type="web_search_20260209",
                    name="web_search",
                    max_uses=self._max_search_uses,
                )
            ],
        )
        self._charge(
            Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        )
        if response.stop_reason == "refusal":
            raise ModelRefusedError(f"the model declined to search for {query!r}")
        reply = ModelReply.model_validate(response.model_dump())
        return SearchFindings(
            results=tuple(
                SearchResult(url=hit.url, title=hit.title)
                for hit in reply.search_hits()
                if hit.url
            )
        )
