import json
import os
import time

import httpx
from pydantic import BaseModel, Field, ValidationError

from osint_harness.model.client import ModelClient, ModelRefusedError, ModelUnavailableError, Usage

DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
NVIDIA_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"


class ChatMessage(BaseModel):
    """The assistant message inside one completion choice.

    Two providers, two names for the same thing: OpenRouter normalises every provider's reasoning
    trace to `reasoning`; NVIDIA's own API, reached directly, calls it `reasoning_content`. Both are
    kept rather than picking one, so nothing about which endpoint served this reply leaks further
    than this one class.
    """

    content: str | None = None
    refusal: str | None = None
    reasoning: str | None = None
    reasoning_content: str | None = None

    def reasoning_trace(self) -> str | None:
        """Whichever field this provider actually used to carry the reasoning trace."""
        return self.reasoning or self.reasoning_content


class ChatChoice(BaseModel):
    """One completion choice, read only for the parts this harness needs."""

    message: ChatMessage = Field(default_factory=ChatMessage)
    finish_reason: str = ""


class ChatUsage(BaseModel):
    """Token accounting in the OpenAI-compatible shape OpenRouter reports."""

    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatError(BaseModel):
    """An error the provider reported inside an otherwise-200 response body."""

    message: str = ""
    code: int | str = ""


class ChatCompletion(BaseModel):
    """A chat completion reduced to the parts this harness reads, typed rather than indexed."""

    choices: list[ChatChoice] = Field(default_factory=list)
    usage: ChatUsage = Field(default_factory=ChatUsage)
    error: ChatError | None = None


class LiveModel(ModelClient):
    """The hosted reasoning engine, reached over an OpenAI-compatible chat completions API.

    Two providers are supported, both fronting the same free model, because they were tried and
    found to behave very differently under this harness's real load. OpenRouter fronts many
    providers behind one key, including models priced at zero, and was the original choice for
    exactly that provider neutrality, but its free tier is rate-limited rather than metered (20
    requests a minute, 50 a day with no credits ever purchased, enforced per account rather than
    per key), and a live run of this harness genuinely exhausted the daily ceiling partway through
    testing. NVIDIA's own API serves the same model directly and tolerated eight back-to-back
    requests with no pacing at all in the same testing session, so it is preferred whenever its key
    is present, falling back to OpenRouter only when it is not. `ModelClient`'s only real
    requirement of a provider, turning a prompt into valid structured JSON, does not care which one
    answers, which is why choosing between them is a matter of which key is configured, not a code
    change.
    """

    MIN_SECONDS_BETWEEN_CALLS_NVIDIA = 0.5
    MIN_SECONDS_BETWEEN_CALLS_OPENROUTER = 3.5

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        *,
        endpoint: str | None = None,
        # ponytail: 16000 covers direction/collection/appraisal/reconciliation live-verified; a
        # Reflection-driven Collection call carrying many accumulated evidence items has been
        # observed to exceed it (truncated JSON, caught as ModelUnavailableError). Raise further,
        # or split the heaviest phases into smaller calls, if this recurs in practice.
        max_tokens: int = 16000,
        reasoning_effort: str = "low",
        timeout_seconds: float = 90.0,
    ) -> None:
        super().__init__()
        nvidia_key = os.environ.get("NVIDIA_API_KEY")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY")
        if api_key is not None:
            key = api_key
            self._endpoint = endpoint if endpoint is not None else NVIDIA_ENDPOINT
        elif nvidia_key:
            key = nvidia_key
            self._endpoint = NVIDIA_ENDPOINT
        elif openrouter_key:
            key = openrouter_key
            self._endpoint = OPENROUTER_ENDPOINT
        else:
            raise ModelUnavailableError(
                "neither NVIDIA_API_KEY nor OPENROUTER_API_KEY is set; export one or pass "
                "api_key= explicitly"
            )
        self._model = model
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._min_seconds_between_calls = (
            self.MIN_SECONDS_BETWEEN_CALLS_NVIDIA
            if self._endpoint == NVIDIA_ENDPOINT
            else self.MIN_SECONDS_BETWEEN_CALLS_OPENROUTER
        )
        headers = {"Authorization": f"Bearer {key}"}
        if self._endpoint == OPENROUTER_ENDPOINT:
            headers["HTTP-Referer"] = "https://github.com/Kritantasasanroy/agentic-osint-harness"
            headers["X-Title"] = "Agentic OSINT Harness"
        self._client = httpx.Client(timeout=timeout_seconds, headers=headers)
        self._last_call_at: float | None = None

    def decide[T: BaseModel](self, purpose: str, system: str, prompt: str, schema: type[T]) -> T:
        self._respect_rate_limit()
        response = self._client.post(
            self._endpoint,
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
        if choice.message.reasoning_trace():
            return (
                f"no reply content returned during {purpose}: the model spent its budget on "
                f"reasoning (finish_reason={choice.finish_reason!r}) without writing an answer; "
                "raise max_tokens or lower reasoning effort"
            )
        return (
            f"no reply content returned during {purpose} "
            f"(finish_reason={choice.finish_reason!r})"
        )

    def _provider_name(self) -> str:
        """Which provider actually served this call, so an error message names it correctly."""
        return "NVIDIA" if self._endpoint == NVIDIA_ENDPOINT else "OpenRouter"

    def _respect_rate_limit(self) -> None:
        """Keep calls paced to whatever this provider was actually measured to tolerate."""
        if self._last_call_at is not None:
            elapsed = time.monotonic() - self._last_call_at
            remaining = self._min_seconds_between_calls - elapsed
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
                f"{self._provider_name()} reported an error during {purpose}: "
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
