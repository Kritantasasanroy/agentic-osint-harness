import pytest
from pydantic import BaseModel, ValidationError

from osint_harness.model.client import (
    ModelUnavailableError,
    ScriptedModel,
    SearchFindings,
    SearchResult,
    Usage,
)
from osint_harness.model.live import ModelReply, WebSearchFailure, WebSearchHit


class Decision(BaseModel):
    verdict: str


class OtherDecision(BaseModel):
    something_else: int


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

    def test_search_returns_scripted_findings_and_charges_for_them(self) -> None:
        model = ScriptedModel()
        findings = SearchFindings(results=(SearchResult(url="https://example.com/a"),))
        model.script_search("Acme Corp", findings)
        assert model.search("Acme Corp") == findings
        assert model.spent().total() > 0

    def test_an_unscripted_search_fails_loudly(self) -> None:
        with pytest.raises(ModelUnavailableError, match="Acme"):
            ScriptedModel().search("Acme")


class TestModelReplyParsing:
    def test_extracts_hits_from_a_search_result_block(self) -> None:
        reply = ModelReply.model_validate(
            {
                "content": [
                    {"type": "text", "text": "I searched."},
                    {
                        "type": "web_search_tool_result",
                        "content": [
                            {"url": "https://reuters.com/a", "title": "Acme files"},
                            {"url": "https://ft.com/b", "title": "Acme raises"},
                        ],
                    },
                ]
            }
        )
        assert reply.search_hits() == (
            WebSearchHit(url="https://reuters.com/a", title="Acme files"),
            WebSearchHit(url="https://ft.com/b", title="Acme raises"),
        )

    def test_ignores_blocks_that_are_not_search_results(self) -> None:
        reply = ModelReply.model_validate(
            {"content": [{"type": "text", "text": "no search happened"}]}
        )
        assert reply.search_hits() == ()

    def test_a_failed_search_raises_rather_than_returning_nothing(self) -> None:
        reply = ModelReply.model_validate(
            {
                "content": [
                    {
                        "type": "web_search_tool_result",
                        "content": {"error_code": "max_uses_exceeded"},
                    }
                ]
            }
        )
        with pytest.raises(ModelUnavailableError, match="max_uses_exceeded"):
            reply.search_hits()

    def test_failure_blocks_are_recognised_as_failures_not_empty_results(self) -> None:
        reply = ModelReply.model_validate(
            {"content": [{"type": "web_search_tool_result", "content": {"error_code": "blocked"}}]}
        )
        assert isinstance(reply.content[0].content, WebSearchFailure)
