from datetime import UTC, datetime

import pytest

from osint_harness.__main__ import CommandLine
from osint_harness.bench.case import BenchmarkCase, DefensibleConfidence
from osint_harness.bench.outcome import BenchmarkRun, CaseOutcome
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
    def _ablation(self) -> Ablation:
        runs = []
        for mode in MemoryMode:
            investigation = Finished.investigation()
            investigation.memory_mode = mode
            case = BenchmarkCase(
                case_id="c1",
                subject=Company(name="Acme Corp"),
                expected=Judgment.SUPPORTED,
                confidence=DefensibleConfidence(lowest=0.6, highest=0.9),
            )
            runs.append(
                BenchmarkRun(
                    memory_mode=mode,
                    outcomes=(CaseOutcome.scored(investigation, case),),
                )
            )
        return Ablation(runs=tuple(runs))

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
