import json

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from osint_harness.model.client import (
    ModelRefusedError,
    ModelUnavailableError,
    ScriptedModel,
    Usage,
)
from osint_harness.model.live import DEFAULT_MODEL, LiveModel


class Decision(BaseModel):
    verdict: str


class OtherDecision(BaseModel):
    something_else: int


class FakeChatResponse:
    """Stands in for an httpx response so tests never reach the network."""

    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict[str, object]:
        return self._payload

    @property
    def is_error(self) -> bool:
        return self.status_code >= httpx.codes.BAD_REQUEST


class Completion:
    """Builds a chat-completion-shaped payload for the fake response."""

    @classmethod
    def of(cls, content: str, finish_reason: str = "stop") -> dict[str, object]:
        return {
            "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40},
        }

    @classmethod
    def refusing(cls) -> dict[str, object]:
        return {
            "choices": [
                {"message": {"content": "", "refusal": "policy"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    @classmethod
    def exhausted_by_reasoning(cls) -> dict[str, object]:
        return {
            "choices": [
                {
                    "message": {"content": "", "reasoning": "thinking at great length..."},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 4000},
        }


class TestUsage:
    def test_total_counts_both_directions(self) -> None:
        assert Usage(input_tokens=100, output_tokens=25).total() == 125

    def test_plus_accumulates(self) -> None:
        combined = Usage(input_tokens=10, output_tokens=1).plus(
            Usage(input_tokens=5, output_tokens=2)
        )
        assert combined == Usage(input_tokens=15, output_tokens=3)

    def test_since_gives_the_spend_between_two_readings(self) -> None:
        before = Usage(input_tokens=100, output_tokens=10)
        after = Usage(input_tokens=250, output_tokens=40)
        assert after.since(before) == Usage(input_tokens=150, output_tokens=30)

    def test_a_negative_delta_is_rejected_rather_than_silently_recorded(self) -> None:
        with pytest.raises(ValidationError):
            Usage(input_tokens=1, output_tokens=1).since(Usage(input_tokens=5, output_tokens=5))


class TestScriptedModel:
    def test_returns_the_scripted_reply_for_a_purpose(self) -> None:
        model = ScriptedModel()
        model.script("direction", Decision(verdict="go"))
        assert model.decide("direction", "sys", "prompt", Decision).verdict == "go"

    def test_charges_tokens_so_efficiency_is_measurable_offline(self) -> None:
        model = ScriptedModel(cost_per_call=Usage(input_tokens=7, output_tokens=3))
        model.script("direction", Decision(verdict="go"))
        before = model.spent()
        model.decide("direction", "sys", "prompt", Decision)
        assert model.spent().since(before) == Usage(input_tokens=7, output_tokens=3)

    def test_an_unscripted_purpose_fails_loudly(self) -> None:
        with pytest.raises(ModelUnavailableError, match="unscripted"):
            ScriptedModel().decide("unscripted", "sys", "prompt", Decision)

    def test_a_reply_of_the_wrong_shape_is_refused(self) -> None:
        model = ScriptedModel()
        model.script("direction", OtherDecision(something_else=1))
        with pytest.raises(ModelUnavailableError, match="Decision"):
            model.decide("direction", "sys", "prompt", Decision)

    def test_records_every_prompt_so_leaks_can_be_asserted_against(self) -> None:
        model = ScriptedModel()
        model.script("direction", Decision(verdict="go"))
        model.decide("direction", "the system prompt", "the user prompt", Decision)
        assert "the system prompt" in model.prompts_seen[0]
        assert "the user prompt" in model.prompts_seen[0]


class TestLiveModelConfiguration:
    def test_requires_an_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(ModelUnavailableError, match="OPENROUTER_API_KEY"):
            LiveModel(api_key=None)

    def test_an_explicit_key_is_accepted_without_the_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        LiveModel(api_key="sk-test")

    def test_the_env_var_is_used_when_no_key_is_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
        LiveModel()


class Recorder:
    """Captures the request body a stubbed client was asked to send, for tests to inspect."""

    def __init__(self, reply: dict[str, object]) -> None:
        self.reply = reply
        self.last_body: dict[str, object] = {}

    def post(self, *_args: object, **kwargs: object) -> FakeChatResponse:
        body = kwargs["json"]
        assert isinstance(body, dict)
        self.last_body = body
        return FakeChatResponse(self.reply)


class TestLiveModelDecide:
    def _stubbed(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], status_code: int = 200
    ) -> LiveModel:
        monkeypatch.setattr("time.sleep", lambda _seconds: None)
        model = LiveModel(api_key="sk-test")
        client = model._client
        monkeypatch.setattr(
            client, "post", lambda *_a, **_k: FakeChatResponse(payload, status_code)
        )
        return model

    def test_a_valid_json_reply_is_parsed_and_validated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = self._stubbed(monkeypatch, Completion.of(json.dumps({"verdict": "go"})))
        assert model.decide("direction", "sys", "prompt", Decision).verdict == "go"

    def test_usage_is_charged_from_the_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = self._stubbed(monkeypatch, Completion.of(json.dumps({"verdict": "go"})))
        model.decide("direction", "sys", "prompt", Decision)
        assert model.spent() == Usage(input_tokens=120, output_tokens=40)

    def test_a_content_filter_finish_reason_is_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = self._stubbed(monkeypatch, Completion.of("", finish_reason="content_filter"))
        with pytest.raises(ModelRefusedError):
            model.decide("direction", "sys", "prompt", Decision)

    def test_a_structured_refusal_field_is_also_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = self._stubbed(monkeypatch, Completion.refusing())
        with pytest.raises(ModelRefusedError):
            model.decide("direction", "sys", "prompt", Decision)

    def test_reasoning_exhausting_the_budget_is_diagnosed_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Observed on a real live run: this model can spend its whole token budget on hidden
        reasoning and never write an answer. The failure must say so, not report a bare silence
        indistinguishable from any other empty reply."""
        model = self._stubbed(monkeypatch, Completion.exhausted_by_reasoning())

        with pytest.raises(ModelUnavailableError, match="spent its budget on reasoning"):
            model.decide("collection", "sys", "prompt", Decision)

    def test_malformed_json_content_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = self._stubbed(monkeypatch, Completion.of("not json at all"))
        with pytest.raises(ModelUnavailableError, match="not valid JSON"):
            model.decide("direction", "sys", "prompt", Decision)

    def test_json_that_does_not_match_the_schema_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = self._stubbed(monkeypatch, Completion.of(json.dumps({"wrong_field": 1})))
        with pytest.raises(ModelUnavailableError, match="Decision"):
            model.decide("direction", "sys", "prompt", Decision)

    def test_an_error_object_in_the_body_is_reported_even_at_200(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = self._stubbed(monkeypatch, {"error": {"message": "boom", "code": 400}})
        with pytest.raises(ModelUnavailableError, match="boom"):
            model.decide("direction", "sys", "prompt", Decision)

    def test_a_rate_limit_response_is_reported_distinctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = self._stubbed(monkeypatch, {}, status_code=429)
        with pytest.raises(ModelUnavailableError, match="rate limited"):
            model.decide("direction", "sys", "prompt", Decision)

    def test_a_plain_http_error_status_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = self._stubbed(monkeypatch, {}, status_code=500)
        with pytest.raises(ModelUnavailableError, match="500"):
            model.decide("direction", "sys", "prompt", Decision)

    def test_the_request_carries_the_model_id_schema_and_both_prompts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("time.sleep", lambda _seconds: None)
        model = LiveModel(api_key="sk-test")
        recorder = Recorder(Completion.of(json.dumps({"verdict": "go"})))
        monkeypatch.setattr(model._client, "post", recorder.post)

        model.decide("direction", "the system line", "the user line", Decision)

        assert recorder.last_body["model"] == DEFAULT_MODEL
        messages = recorder.last_body["messages"]
        assert isinstance(messages, list)
        assert "the system line" in messages[0]["content"]
        assert "verdict" in messages[0]["content"]
        assert messages[1]["content"] == "the user line"

    def test_two_calls_are_paced_to_respect_the_free_tier_rate_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr("time.sleep", slept.append)
        clock = iter([1000.0, 1000.5, 1000.5])
        monkeypatch.setattr("time.monotonic", lambda: next(clock))
        model = LiveModel(api_key="sk-test")
        monkeypatch.setattr(
            model._client,
            "post",
            lambda *_a, **_k: FakeChatResponse(Completion.of(json.dumps({"verdict": "go"}))),
        )

        model.decide("direction", "sys", "prompt", Decision)
        model.decide("direction", "sys", "prompt", Decision)

        assert slept and slept[0] > 0
