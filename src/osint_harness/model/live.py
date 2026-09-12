import json
import os
import time

import httpx
from pydantic import BaseModel, Field, ValidationError

from osint_harness.model.client import ModelClient, ModelRefusedError, ModelUnavailableError, Usage

DEFAULT_MODEL = "nex-agi/nex-n2.5-pro:free"


class ChatMessage(BaseModel):
    """The assistant message inside one completion choice."""

    content: str | None = None
    refusal: str | None = None
    reasoning: str | None = None


class ChatChoice(BaseModel):
    """One completion choice, read only for the parts this harness needs."""

    message: ChatMessage = Field(default_factory=ChatMessage)
    finish_reason: str = ""


class ChatUsage(BaseModel):
    """Token accounting in the OpenAI-compatible shape OpenRouter reports."""

    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatError(BaseModel):
    """An error OpenRouter reported inside an otherwise-200 response body."""

    message: str = ""
    code: int | str = ""


class ChatCompletion(BaseModel):
    """A chat completion reduced to the parts this harness reads, typed rather than indexed."""

    choices: list[ChatChoice] = Field(default_factory=list)
    usage: ChatUsage = Field(default_factory=ChatUsage)
    error: ChatError | None = None


class LiveModel(ModelClient):
    """The hosted reasoning engine, reached over OpenRouter's OpenAI-compatible chat API.

    OpenRouter fronts many providers behind one key, including models priced at zero, which is why
    it was chosen over a single vendor's API: the harness's only requirement of a model is that it
    follows instructions and returns valid JSON, not that it comes from any particular lab.

    The free tier is rate-limited rather than metered (20 requests/minute, 50/day with no credits
    ever purchased, 1000/day past $10 lifetime), enforced per account rather than per key. Calls are
    paced to stay under the per-minute ceiling; the daily ceiling is a real constraint this harness
    cannot lift, which is why a full multi-mode benchmark sweep is not attempted against it in one
    sitting — see the report for what was actually verified live.
    """

    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
    MIN_SECONDS_BETWEEN_CALLS = 3.5

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        # ponytail: 16000 covers direction/collection/appraisal/reconciliation live-verified; a
        # Reflection-driven Collection call carrying many accumulated evidence items has been
        # observed to exceed it (truncated JSON, caught as ModelUnavailableError). Raise further,
        # or split the heaviest phases into smaller calls, if this recurs in practice.
        max_tokens: int = 16000,
        reasoning_effort: str = "low",
        timeout_seconds: float = 90.0,
    ) -> None:
        super().__init__()
        key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise ModelUnavailableError(
                "OPENROUTER_API_KEY is not set; export it or pass api_key= explicitly"
            )
        self._model = model
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "https://github.com/Kritantasasanroy/agentic-osint-harness",
                "X-Title": "Agentic OSINT Harness",
            },
        )
        self._last_call_at: float | None = None

    def decide[T: BaseModel](self, purpose: str, system: str, prompt: str, schema: type[T]) -> T:
        self._respect_rate_limit()
        response = self._client.post(
            self.ENDPOINT,
            json={
                "model": self._model,
                "max_tokens": self._max_tokens,
                "response_format": {"type": "json_object"},
                "reasoning": {"effort": self._reasoning_effort},
                "messages": [
                    {"role": "system", "content": self._json_instruction(system, schema)},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        completion = self._parsed(response, purpose)
        choice = self._only_choice(completion, purpose)
        if choice.finish_reason == "content_filter" or choice.message.refusal:
            raise ModelRefusedError(f"the model declined to answer during {purpose}")
        content = choice.message.content
        if not content or not content.strip():
            raise ModelUnavailableError(self._empty_reply_reason(purpose, choice))
        return self._validated(content, schema, purpose)

    def _empty_reply_reason(self, purpose: str, choice: ChatChoice) -> str:
        """A diagnosable reason for an empty reply, distinguishing exhaustion from silence."""
        if choice.message.reasoning:
            return (
                f"no reply content returned during {purpose}: the model spent its budget on "
                f"reasoning (finish_reason={choice.finish_reason!r}) without writing an answer; "
                "raise max_tokens or lower reasoning effort"
            )
        return (
            f"no reply content returned during {purpose} "
            f"(finish_reason={choice.finish_reason!r})"
        )

    def _respect_rate_limit(self) -> None:
        """Keep this account under the free tier's 20-requests-per-minute ceiling."""
        if self._last_call_at is not None:
            elapsed = time.monotonic() - self._last_call_at
            remaining = self.MIN_SECONDS_BETWEEN_CALLS - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_call_at = time.monotonic()

    def _json_instruction(self, system: str, schema: type[BaseModel]) -> str:
        """The standing instruction plus the exact shape the reply must take."""
        return (
            f"{system}\n\n"
            "Respond with a single JSON object and nothing else — no prose, no markdown fences. "
            "It must validate against this JSON Schema:\n"
            f"{json.dumps(schema.model_json_schema())}"
        )

    def _parsed(self, response: httpx.Response, purpose: str) -> ChatCompletion:
        """The raw HTTP response, turned into a typed completion or a clear failure."""
        try:
            body = response.json()
        except ValueError as failure:
            raise ModelUnavailableError(
                f"non-JSON response during {purpose}: {response.status_code}"
            ) from failure
        completion = ChatCompletion.model_validate(body)
        if completion.error is not None:
            raise ModelUnavailableError(
                f"OpenRouter reported an error during {purpose}: "
                f"{completion.error.message or completion.error.code}"
            )
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise ModelUnavailableError(f"rate limited during {purpose}: {response.text[:200]}")
        if response.is_error:
            raise ModelUnavailableError(
                f"HTTP {response.status_code} during {purpose}: {response.text[:200]}"
            )
        self._charge(
            Usage(
                input_tokens=completion.usage.prompt_tokens,
                output_tokens=completion.usage.completion_tokens,
            )
        )
        return completion

    def _only_choice(self, completion: ChatCompletion, purpose: str) -> ChatChoice:
        if not completion.choices:
            raise ModelUnavailableError(f"no choices returned during {purpose}")
        return completion.choices[0]

    def _validated[T: BaseModel](self, content: str, schema: type[T], purpose: str) -> T:
        """Parse and validate the model's JSON reply against the schema the phase asked for."""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as failure:
            raise ModelUnavailableError(
                f"reply during {purpose} was not valid JSON: {content[:200]!r}"
            ) from failure
        try:
            return schema.model_validate(payload)
        except ValidationError as failure:
            raise ModelUnavailableError(
                f"reply during {purpose} did not match {schema.__name__}: {failure}"
            ) from failure
