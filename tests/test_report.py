from datetime import UTC, datetime

import pytest

from osint_harness.__main__ import CommandLine
from osint_harness.bench.case import BenchmarkCase, DefensibleConfidence
from osint_harness.bench.outcome import BenchmarkRun, CaseOutcome, FailureMode
from osint_harness.domain.analysis import (
    Assessment,
    Consistency,
    Evidence,
    Findings,
    Hypothesis,
    Judgment,
)
from osint_harness.domain.investigation import (
    Investigation,
    InvestigationPhase,
    Lead,
    MemoryMode,
    Step,
    ToolCall,
)
from osint_harness.domain.provenance import (
    Document,
    InformationCredibility,
    SourceReliability,
)
from osint_harness.domain.subject import Company
from osint_harness.report.ablation import Ablation
from osint_harness.report.dossier import Dossier


class Finished:
    """Builds a completed investigation with real citations, for the renderers to work on."""

    ARTICLE = "https://reuters.com/acme"

    @classmethod
    def investigation(cls) -> Investigation:
        investigation = Investigation.open(
            run_id="acme-short", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        investigation.record_document(
            Document.retrieved(
                url=cls.ARTICLE,
                title="Acme files accounts",
                text="Acme Corp filed accounts for 2024.",
                retrieved_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
        )
        investigation.grade_source(
            "reuters.com", SourceReliability.COMPLETELY_RELIABLE, "major wire service"
        )
        identifier = investigation.record_evidence(
            Evidence(
                assertion="Acme Corp filed accounts for 2024.",
                document_url=cls.ARTICLE,
                source_domain="reuters.com",
                credibility=InformationCredibility.CONFIRMED,
            )
        )
        investigation.hypotheses.extend(
            [
                Hypothesis(
                    statement="Acme is operating.",
                    consistency={identifier: Consistency.CONSISTENT},
                ),
                Hypothesis(
                    statement="Acme is dormant.",
                    consistency={identifier: Consistency.INCONSISTENT},
                ),
            ]
        )
        investigation.leads.append(Lead(question="Who owns it?"))
        investigation.assess(
            Assessment(
                judgment=Judgment.SUPPORTED,
                leading_hypothesis="Acme is operating.",
                probability=0.82,
                rationale="Filings corroborate the description.",
            )
        )
        investigation.record_findings(
            Findings(
                summary="Acme is an operating company.",
                key_findings=("It filed accounts for 2024.",),
                gaps=("Ownership is unclear.",),
                conflicts=("One outlet reported a closure that filings contradict.",),
            )
        )
        investigation.record_step(
            Step(
                index=0,
                phase=InvestigationPhase.DIRECTION,
                moved_to=InvestigationPhase.COLLECTION,
                reason="opened with 2 competing hypotheses",
                input_tokens=400,
                output_tokens=100,
            )
        )
        return investigation


class TestDossier:
    def _rendered(self) -> str:
        return Dossier(Finished.investigation()).as_markdown()

    def test_leads_with_the_verdict_and_its_confidence_band(self) -> None:
        rendered = self._rendered()

        assert "# Findings — Acme Corp" in rendered
        assert "supported" in rendered
        assert "very likely" in rendered
        assert "0.82" in rendered

    def test_states_the_proposition_the_verdict_is_a_verdict_on(self) -> None:
        assert "**Proposition under test:** Acme Corp is a real entity" in self._rendered()

    def test_every_evidence_item_carries_a_grade_and_a_resolvable_citation(self) -> None:
        rendered = self._rendered()

        assert "| Reliability | Credibility | Citation |" in rendered
        assert Finished.ARTICLE in rendered
        assert "reuters.com" in rendered

    def test_hypotheses_are_shown_ranked_by_disconfirmation(self) -> None:
        rendered = self._rendered()

        assert "Competing hypotheses" in rendered
        assert "least disconfirmed comes first" in rendered
        assert rendered.index("Acme is operating.") < rendered.index("Acme is dormant.")

    def test_conflicts_and_open_questions_are_reported_not_buried(self) -> None:
        rendered = self._rendered()

        assert "Where sources disagree" in rendered
        assert "What remains unknown" in rendered
        assert "Unanswered: Who owns it?" in rendered

    def test_the_investigation_log_is_included(self) -> None:
        rendered = self._rendered()

        assert "Investigation log" in rendered
        assert "opened with 2 competing hypotheses" in rendered

    def test_an_empty_investigation_still_renders_without_claiming_anything(self) -> None:
        bare = Investigation.open(
            run_id="r", subject=Company(name="Acme"), memory_mode=MemoryMode.NONE
        )

        rendered = Dossier(bare).as_markdown()

        assert "No evidence was gathered." in rendered
        assert "insufficient evidence" in rendered

    def test_the_json_record_round_trips_back_into_an_investigation(self) -> None:
        original = Finished.investigation()

        restored = Investigation.model_validate_json(Dossier(original).as_json())

        assert restored.run_id == original.run_id
        assert restored.latest_assessment().judgment is Judgment.SUPPORTED
        assert len(restored.evidence) == len(original.evidence)


class TestAblation:
    def _case(self) -> BenchmarkCase:
        return BenchmarkCase(
            case_id="c1",
            subject=Company(name="Acme Corp"),
            expected=Judgment.SUPPORTED,
            confidence=DefensibleConfidence(lowest=0.6, highest=0.9),
        )

    def _ablation(self) -> Ablation:
        runs = []
        for mode in MemoryMode:
            investigation = Finished.investigation()
            investigation.memory_mode = mode
            runs.append(
                BenchmarkRun(
                    memory_mode=mode,
                    outcomes=(CaseOutcome.scored(investigation, self._case()),),
                )
            )
        return Ablation(runs=tuple(runs))

    def _longer_investigation(self) -> Investigation:
        """The finished investigation plus one more step, whose lookup failed, and assessment."""
        investigation = Finished.investigation()
        investigation.record_step(
            Step(
                index=1,
                phase=InvestigationPhase.COLLECTION,
                moved_to=InvestigationPhase.APPRAISAL,
                reason="the registry lookup failed",
                tool_calls=(
                    ToolCall(
                        tool="search", query="Acme Corp", documents_returned=0, succeeded=False
                    ),
                ),
            )
        )
        investigation.assess(
            Assessment(
                judgment=Judgment.SUPPORTED,
                leading_hypothesis="Acme is operating.",
                probability=0.9,
                rationale="Filings still corroborate the description.",
            )
        )
        return investigation

    def test_efficiency_reports_steps_and_tool_calls_but_not_latency(self) -> None:
        rendered = self._ablation().as_markdown()

        assert "| Mean steps | Mean tool calls | Failed tool calls |" in rendered
        assert "| 1.00 | 0.00 | 0 |" in rendered
        assert "Latency |" not in rendered
        assert "separate a source outage from a reasoning failure" in rendered

    def test_reports_mean_cumulative_reward_after_each_step(self) -> None:
        ablation = self._ablation()
        rendered = ablation.as_markdown()
        first = ablation.run_for(MemoryMode.NONE).mean_reward_progression()[0]

        assert "### Reward progression" in rendered
        assert "| Step | `none` | `short` | `long` |" in rendered
        assert f"| 1 | {first:.3f} | {first:.3f} | {first:.3f} |" in rendered
        assert "keeps paying for steps that add nothing" in rendered

    def test_reports_mean_confidence_after_each_assessment_from_the_baseline(self) -> None:
        ablation = self._ablation()
        rendered = ablation.as_markdown()

        assert "### Confidence progression" in rendered
        assert "| Assessment | `none` | `short` | `long` |" in rendered
        assert "| 1 | 0.50 | 0.50 | 0.50 |" in rendered
        assert "| 2 | 0.82 | 0.82 | 0.82 |" in rendered
        assert ablation.run_for(MemoryMode.LONG).mean_confidence_progression() == pytest.approx(
            (0.5, 0.82)
        )
        assert "asserted once and left" in rendered

    def test_an_episode_that_stopped_sooner_holds_its_final_value_in_the_means(self) -> None:
        brief = CaseOutcome.scored(Finished.investigation(), self._case())
        extended = CaseOutcome.scored(self._longer_investigation(), self._case())

        run = BenchmarkRun(memory_mode=MemoryMode.SHORT, outcomes=(brief, extended))

        assert run.mean_confidence_progression() == pytest.approx((0.5, 0.82, 0.86))
        assert run.mean_reward_progression()[1] == pytest.approx(
            (brief.reward_progression()[-1] + extended.reward_progression()[1]) / 2
        )
        assert (run.mean_steps(), run.mean_tool_calls(), run.failed_tool_calls()) == (1.5, 0.5, 1)

    def test_an_outcome_recorded_before_this_field_existed_is_excluded_not_zeroed(self) -> None:
        """A `CaseOutcome` loaded from disk before `confidence_series` was added defaults to an
        empty tuple, not a missing episode. Averaging it in as a 0.0 at every position would
        silently drag every mean down by a fixed fraction for as long as any old file sits
        alongside new ones, which is exactly the shape of bug a flat, wrong-looking number never
        announces itself as."""
        current = CaseOutcome.scored(Finished.investigation(), self._case())
        legacy = current.model_copy(update={"confidence_series": ()})

        run = BenchmarkRun(memory_mode=MemoryMode.SHORT, outcomes=(legacy, current))

        assert run.mean_confidence_progression() == current.confidence_series

    def test_a_mode_no_episode_of_which_ran_that_long_shows_a_dash(self) -> None:
        ablation = Ablation(
            runs=(
                BenchmarkRun(
                    memory_mode=MemoryMode.NONE,
                    outcomes=(CaseOutcome.scored(Finished.investigation(), self._case()),),
                ),
                BenchmarkRun(
                    memory_mode=MemoryMode.LONG,
                    outcomes=(CaseOutcome.scored(self._longer_investigation(), self._case()),),
                ),
            )
        )

        rendered = ablation.as_markdown()

        assert "| 2 | — | " in rendered
        assert "| 3 | — | 0.90 |" in rendered

    def test_reports_every_mode_that_was_run(self) -> None:
        rendered = self._ablation().as_markdown()

        for mode in MemoryMode:
            assert f"`{mode.value}`" in rendered

    def test_states_why_the_comparison_is_valid_at_all(self) -> None:
        rendered = self._ablation().as_markdown()

        assert "byte-identical retrieval" in rendered

    def test_separates_irrelevant_retrieval_from_harmful(self) -> None:
        rendered = self._ablation().as_markdown()

        assert "Irrelevant retrieval" in rendered
        assert "Harmful retrieval" in rendered
        assert "not a demonstrated cause" in rendered

    def test_lists_every_failure_mode_and_labels_tokens_as_estimates(self) -> None:
        rendered = self._ablation().as_markdown()

        for failure in FailureMode:
            assert f"| {failure.value.replace('_', ' ')} |" in rendered
        assert "not provider-measured usage" in rendered

    def test_asking_for_a_mode_that_was_not_run_is_an_error(self) -> None:
        ablation = Ablation(runs=())

        with pytest.raises(KeyError, match="no run for memory mode"):
            ablation.run_for(MemoryMode.LONG)


class TestCommandLineParsing:
    def test_flags_are_accepted_before_the_subcommand(self) -> None:
        invocation = CommandLine.invocation(("--record", "investigate", "some-case"))

        assert invocation.record
        assert invocation.command == "investigate"
        assert invocation.case_id == "some-case"

    def test_flags_are_accepted_after_the_subcommand(self) -> None:
        invocation = CommandLine.invocation(("investigate", "some-case", "--record"))

        assert invocation.record
        assert invocation.case_id == "some-case"

    def test_memory_mode_is_parsed_into_the_enum_not_left_a_string(self) -> None:
        invocation = CommandLine.invocation(("bench", "--memory", "long"))

        assert invocation.memory is MemoryMode.LONG

    def test_replay_and_the_rehearsed_analyst_are_the_defaults(self) -> None:
        invocation = CommandLine.invocation(("bench",))

        assert not invocation.record
        assert not invocation.live
        assert invocation.memory is MemoryMode.SHORT

    def test_the_step_ceiling_is_configurable_and_becomes_a_budget(self) -> None:
        invocation = CommandLine.invocation(("--max-steps", "8", "ablate"))

        assert invocation.budget().max_steps == 8

    def test_an_unknown_command_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            CommandLine.invocation(("not-a-command",))
