"""Regression tests, one per finding from the independent gate audit.

Each test names the defect it exists to prevent. They are kept together rather than scattered so
that the cost of the audit is visible, and so a future change that quietly reintroduces one of
these fails against a test that says plainly what was wrong the first time.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from osint_harness.bench.reward import StepReward
from osint_harness.domain.analysis import Consistency, Evidence, Hypothesis, Judgment
from osint_harness.domain.investigation import (
    Investigation,
    InvestigationPhase,
    MemoryMode,
    Step,
    UngroundedEvidenceError,
)
from osint_harness.domain.provenance import (
    Document,
    InformationCredibility,
    SourceReliability,
)
from osint_harness.domain.subject import Company
from osint_harness.graph.briefing import Briefing
from osint_harness.graph.phases import Collection, Dissemination, Reconciliation
from osint_harness.graph.schemas import CollectionPlan, ReconciliationResult, ReportDraft
from osint_harness.model.client import ModelUnavailableError, ScriptedModel, SearchFindings
from osint_harness.report.dossier import Dossier
from osint_harness.sources.cassette import Cassette, CassetteMode
from osint_harness.sources.tools import Tool


class Episode:
    """Builds an investigation whose evidence was gathered in an earlier round."""

    ARTICLE = "https://reuters.com/a"
    ASSERTION = "Acme was dissolved in 2019."

    @classmethod
    def document(cls) -> Document:
        return Document.retrieved(
            url=cls.ARTICLE,
            title="t",
            text="body",
            retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    @classmethod
    def with_stale_evidence(cls, mode: MemoryMode) -> Investigation:
        """Evidence extracted in an earlier round, with a later step that added none."""
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=mode
        )
        investigation.record_document(cls.document())
        investigation.grade_source("reuters.com", SourceReliability.USUALLY_RELIABLE, "wire")
        identifier = investigation.record_evidence(
            Evidence(
                assertion=cls.ASSERTION,
                document_url=cls.ARTICLE,
                source_domain="reuters.com",
                credibility=InformationCredibility.CONFIRMED,
            )
        )
        investigation.record_step(cls._step(0, (identifier,), InvestigationPhase.APPRAISAL))
        investigation.record_step(cls._step(1, (), InvestigationPhase.REFLECTION))
        return investigation

    @classmethod
    def _step(
        cls, index: int, evidence: tuple[str, ...], phase: InvestigationPhase
    ) -> Step:
        return Step(
            index=index,
            phase=phase,
            moved_to=InvestigationPhase.RECONCILIATION,
            reason="earlier round",
            evidence_added=evidence,
        )


class SilentSearchFailure(ScriptedModel):
    """A model whose web search is unavailable, to prove the failure is recorded not swallowed."""

    def search(self, _query: str) -> SearchFindings:
        raise ModelUnavailableError("search backend unavailable")


class DeadTool(Tool):
    """A source that returns nothing, so collection has only the search to report."""

    @property
    def name(self) -> str:
        return "dead"

    def retrieve(self, _query: str) -> tuple[Document, ...]:
        return ()


class TestNoneIsActuallyABaseline:
    """Gate finding: Reconciliation and Dissemination read evidence outside the memory gate,
    so the `none` arm leaked the state it is defined by withholding. The original test asserted
    on `Briefing.header()`, which was gated, instead of the prompt actually sent."""

    def test_short_carries_evidence_from_an_earlier_round(self) -> None:
        briefing = Briefing(Episode.with_stale_evidence(MemoryMode.SHORT))

        assert Episode.ASSERTION in briefing.evidence()

    def test_none_withholds_evidence_from_an_earlier_round(self) -> None:
        briefing = Briefing(Episode.with_stale_evidence(MemoryMode.NONE))

        assert briefing.visible_evidence() == {}
        assert briefing.evidence() == ""

    def test_none_withholds_it_from_the_prompt_that_is_actually_sent(self) -> None:
        investigation = Episode.with_stale_evidence(MemoryMode.NONE)
        model = ScriptedModel()
        model.script("reconciliation", ReconciliationResult(judgment=Judgment.SUPPORTED))

        Reconciliation(model).advance(investigation)

        assert model.prompts_seen
        assert Episode.ASSERTION not in "\n".join(model.prompts_seen)

    def test_short_does_reach_the_prompt_that_is_actually_sent(self) -> None:
        investigation = Episode.with_stale_evidence(MemoryMode.SHORT)
        model = ScriptedModel()
        model.script("reconciliation", ReconciliationResult(judgment=Judgment.SUPPORTED))

        Reconciliation(model).advance(investigation)

        assert Episode.ASSERTION in "\n".join(model.prompts_seen)


class TestGroundingHoldsByConstruction:
    """Gate finding: the citation guarantee was enforced only by `record_evidence`'s discipline.
    An `Investigation` could be constructed or deserialised directly holding forged evidence, and
    the dossier rendered it as a clean citation."""

    def _forged(self) -> Evidence:
        return Evidence(
            assertion="Invented.",
            document_url="https://never-retrieved.example/smear",
            source_domain="never-retrieved.example",
            credibility=InformationCredibility.CONFIRMED,
        )

    def test_an_investigation_cannot_be_constructed_holding_a_forged_citation(self) -> None:
        forged = self._forged()

        with pytest.raises(ValidationError, match="no retrieved document"):
            Investigation(
                run_id="r1",
                subject=Company(name="Acme Corp"),
                memory_mode=MemoryMode.SHORT,
                evidence={forged.identifier: forged},
            )

    def test_an_investigation_cannot_exist_without_a_baseline_assessment(self) -> None:
        """Gate finding: scoring read `assessments[-1]` on a directly constructed record and
        raised IndexError, which crashed the sweep instead of tagging a failed episode."""
        with pytest.raises(ValidationError, match="no assessment"):
            Investigation(
                run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
            )

    def test_a_forged_record_cannot_be_reloaded_from_disk(self) -> None:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        forged = self._forged()
        investigation.evidence[forged.identifier] = forged
        serialised = investigation.model_dump_json()

        with pytest.raises(ValidationError, match="no retrieved document"):
            Investigation.model_validate_json(serialised)

    def test_dissemination_refuses_to_conclude_on_a_forged_citation(self) -> None:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        forged = self._forged()
        investigation.evidence[forged.identifier] = forged
        model = ScriptedModel()
        model.script("dissemination", ReportDraft(summary="anything"))

        with pytest.raises(UngroundedEvidenceError, match="refusing to report"):
            Dissemination(model).advance(investigation)


class TestTheNarrativeIsGroundedToo:
    """Gate finding: Dissemination validated the evidence table but not the written narrative,
    which is free model text and could carry an invented link."""

    def test_a_link_the_narrative_invented_is_refused(self) -> None:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        model = ScriptedModel()
        model.script(
            "dissemination",
            ReportDraft(
                summary="See https://invented.example/proof for confirmation.",
                key_findings=("A finding.",),
            ),
        )

        with pytest.raises(UngroundedEvidenceError, match="narrative cites"):
            Dissemination(model).advance(investigation)

    def test_a_link_that_was_actually_retrieved_is_allowed(self) -> None:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        investigation.record_document(Episode.document())
        model = ScriptedModel()
        model.script(
            "dissemination",
            ReportDraft(summary=f"Reported at {Episode.ARTICLE}.", key_findings=("A finding.",)),
        )

        transition = Dissemination(model).advance(investigation)

        assert transition.next_phase is InvestigationPhase.COMPLETE


class TestAGradeCarriesItsReason:
    """Gate finding: the model produced a justification for every Admiralty grade and the code
    discarded it, leaving the report showing a bare letter."""

    def test_the_reason_survives_onto_the_investigation(self) -> None:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )

        investigation.grade_source(
            "reuters.com", SourceReliability.COMPLETELY_RELIABLE, "major wire service"
        )

        assert investigation.source_for("reuters.com").reason == "major wire service"

    def test_the_reason_is_shown_beside_the_grade_in_the_report(self) -> None:
        investigation = Episode.with_stale_evidence(MemoryMode.SHORT)

        assert "B — wire" in Dossier(investigation).as_markdown()

    def test_an_ungraded_publisher_reports_as_unjudged_rather_than_as_poor(self) -> None:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )

        source = investigation.source_for("never-seen.example")

        assert source.reliability is SourceReliability.CANNOT_BE_JUDGED
        assert source.reason == "not yet assessed"


class TestTheRendererIsAlsoAGate:
    """Re-audit finding: this check was removed on the reasoning that the constructor validator
    made it redundant. It does not — pydantic does not revalidate on mutation into a held dict, so
    a forged item assigned directly still rendered as a clean citation. Restored, and pinned here
    so it is not optimised away a second time."""

    def test_a_forged_citation_assigned_directly_is_refused_at_render(self) -> None:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        forged = Evidence(
            assertion="Invented.",
            document_url="https://never-retrieved.example/smear",
            source_domain="never-retrieved.example",
            credibility=InformationCredibility.CONFIRMED,
        )
        investigation.evidence[forged.identifier] = forged

        with pytest.raises(UngroundedEvidenceError, match="refusing to render"):
            Dossier(investigation).as_markdown()

    def test_the_json_record_is_guarded_too_not_just_the_markdown(self) -> None:
        """Second re-audit finding: the first fix guarded `as_markdown` only, leaving `as_json` —
        the record the harness calls its recomputability source of truth — safe purely because one
        caller happened to invoke markdown first."""
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        forged = Evidence(
            assertion="Invented.",
            document_url="https://never-retrieved.example/smear",
            source_domain="never-retrieved.example",
            credibility=InformationCredibility.CONFIRMED,
        )
        investigation.evidence[forged.identifier] = forged

        with pytest.raises(UngroundedEvidenceError, match="refusing to render"):
            Dossier(investigation).as_json()

    def test_a_grounded_investigation_still_renders_both_ways(self) -> None:
        dossier = Dossier(Episode.with_stale_evidence(MemoryMode.SHORT))

        assert Episode.ARTICLE in dossier.as_markdown()
        assert Episode.ARTICLE in dossier.as_json()


class TestReflectionsHypothesesAreGatedToo:
    """Re-audit finding: hypotheses were left entirely ungated, but Reflection adds hypotheses
    worded from evidence seen in an earlier round, so those additions carried that round's findings
    into later prompts under NONE even though the evidence itself was hidden."""

    def _with_both(self, mode: MemoryMode) -> Briefing:
        investigation = Episode.with_stale_evidence(mode)
        investigation.hypotheses.append(
            Hypothesis(statement="Stated at the outset.", origin="direction")
        )
        investigation.hypotheses.append(
            Hypothesis(statement="Derived from what round one found.", origin="reflection")
        )
        return Briefing(investigation)

    def test_short_shows_both(self) -> None:
        rendered = self._with_both(MemoryMode.SHORT).hypotheses()

        assert "Stated at the outset." in rendered
        assert "Derived from what round one found." in rendered

    def test_none_shows_only_the_opening_set(self) -> None:
        rendered = self._with_both(MemoryMode.NONE).hypotheses()

        assert "Stated at the outset." in rendered
        assert "Derived from what round one found." not in rendered


class TestAnUntestedHypothesisCannotWinByDefault:
    """Re-audit finding, introduced by my own fix: gating hypotheses under NONE meant a
    Reflection-added one could never be judged, so it kept a disconfirming score of zero — and
    under a naive least-disconfirmed ranking that beats every hypothesis actually examined. A guess
    nobody checked would have led the report."""

    def _with_one_tested_and_one_not(self) -> Investigation:
        investigation = Episode.with_stale_evidence(MemoryMode.SHORT)
        identifier = next(iter(investigation.evidence))
        investigation.hypotheses.append(
            Hypothesis(
                statement="Examined, and contradicted once.",
                consistency={identifier: Consistency.INCONSISTENT},
            )
        )
        investigation.hypotheses.append(
            Hypothesis(statement="Never examined at all.", origin="reflection")
        )
        return investigation

    def test_the_untested_hypothesis_ranks_last_despite_scoring_zero(self) -> None:
        ranked = self._with_one_tested_and_one_not().ranked_hypotheses()

        assert ranked[-1].statement == "Never examined at all."

    def test_it_is_never_reported_as_the_leading_explanation(self) -> None:
        investigation = self._with_one_tested_and_one_not()

        assert investigation.leading_hypothesis() == "Examined, and contradicted once."

    def test_nothing_leads_while_nothing_has_been_tested(self) -> None:
        investigation = Episode.with_stale_evidence(MemoryMode.SHORT)
        investigation.hypotheses.append(Hypothesis(statement="Untested."))

        assert investigation.leading_hypothesis() == ""

    def test_an_untested_hypothesis_is_still_shown_and_marked(self) -> None:
        rendered = Dossier(self._with_one_tested_and_one_not()).as_markdown()

        assert "Never examined at all." in rendered
        assert "NOT TESTED" in rendered


class TestEvidenceIsPaidForOnce:
    """Gate finding: `information_gain` summed occurrences rather than distinct items. Because
    evidence identifiers are content-derived, a repeated extraction returned the same identifier,
    and the reward could exceed the evidence the episode actually held."""

    def _investigation(self, repeats: int) -> Investigation:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        investigation.record_document(Episode.document())
        investigation.grade_source(
            "reuters.com", SourceReliability.COMPLETELY_RELIABLE, "wire service"
        )
        identifier = investigation.record_evidence(
            Evidence(
                assertion=Episode.ASSERTION,
                document_url=Episode.ARTICLE,
                source_domain="reuters.com",
                credibility=InformationCredibility.CONFIRMED,
            )
        )
        investigation.record_step(
            Episode._step(0, (identifier,) * repeats, InvestigationPhase.APPRAISAL)
        )
        return investigation

    def test_a_repeated_identifier_in_one_step_is_credited_once(self) -> None:
        once = StepReward.series(self._investigation(1))[0]
        thrice = StepReward.series(self._investigation(3))[0]

        assert thrice.information_gain == once.information_gain

    def test_credit_never_exceeds_the_evidence_actually_held(self) -> None:
        investigation = self._investigation(3)

        gained = sum(reward.information_gain for reward in StepReward.series(investigation))

        assert gained <= investigation.total_evidence_weight()

    def test_evidence_reported_again_in_a_later_step_earns_nothing_twice(self) -> None:
        investigation = self._investigation(1)
        repeated = next(iter(investigation.evidence))
        investigation.record_step(
            Episode._step(1, (repeated,), InvestigationPhase.APPRAISAL)
        )

        rewards = StepReward.series(investigation)

        assert rewards[0].information_gain > 0.0
        assert rewards[1].information_gain == 0.0


class TestSearchFailuresAreRecorded:
    """Gate finding: `_search` hard-coded `succeeded=True` and caught nothing, so a failing search
    either crashed the episode or was logged as a success and could never raise TOOL_FAILURE."""

    def test_a_failing_search_is_recorded_as_a_failed_lookup(self) -> None:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        model = SilentSearchFailure()
        model.script("collection", CollectionPlan(search_queries=("Acme Corp",)))
        model.script("reading_choice", ReportDraft())
        sources = DeadTool(Cassette(mode=CassetteMode.RECORD))

        transition = Collection(model, sources, sources).advance(investigation)

        searches = [call for call in transition.tool_calls if call.tool == "web_search"]
        assert searches
        assert searches[0].succeeded is False
        assert "unavailable" in searches[0].failure_reason
