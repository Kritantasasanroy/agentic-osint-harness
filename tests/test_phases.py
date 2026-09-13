from datetime import UTC, datetime

import httpx
import pytest

from osint_harness.domain.analysis import Consistency, Evidence, Judgment
from osint_harness.domain.investigation import (
    Budget,
    Investigation,
    InvestigationPhase,
    Lead,
    LeadPriority,
    MemoryMode,
    Recollection,
    Step,
    UngroundedEvidenceError,
)
from osint_harness.domain.provenance import (
    Document,
    InformationCredibility,
    SourceReliability,
)
from osint_harness.domain.subject import Claim, Company
from osint_harness.graph.briefing import Briefing
from osint_harness.graph.machine import InvestigationGraph, Node
from osint_harness.graph.phases import (
    Appraisal,
    Collection,
    Direction,
    Dissemination,
    Reconciliation,
    Reflection,
)
from osint_harness.graph.schemas import (
    AppraisalResult,
    CollectionPlan,
    ConsistencyCall,
    DirectionPlan,
    ExtractedAssertion,
    PlannedLead,
    ReadingChoice,
    ReconciliationResult,
    ReflectionResult,
    ReportDraft,
    SourceGrading,
)
from osint_harness.model.client import ScriptedModel
from osint_harness.sources.cassette import Cassette, CassetteMode
from osint_harness.sources.tools import Tool


class Episode:
    """Builds the investigations, documents and scripted replies the phase tests run against."""

    ARTICLE = "https://reuters.com/acme-files-accounts"
    RETRIEVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    @classmethod
    def document(cls) -> Document:
        return Document.retrieved(
            url=cls.ARTICLE,
            title="Acme files",
            text="Acme Corp filed accounts for 2024.",
            retrieved_at=cls.RETRIEVED_AT,
        )

    @classmethod
    def opened(
        cls,
        mode: MemoryMode = MemoryMode.SHORT,
        budget: Budget | None = None,
    ) -> Investigation:
        return Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=mode, budget=budget
        )

    @classmethod
    def with_document(cls) -> Investigation:
        investigation = cls.opened()
        investigation.record_document(cls.document())
        return investigation

    @classmethod
    def forged_evidence(cls) -> Evidence:
        return Evidence(
            assertion="Never retrieved.",
            document_url="https://nowhere.example/x",
            source_domain="nowhere.example",
            credibility=InformationCredibility.CONFIRMED,
        )

    @classmethod
    def placeholder_step(cls, index: int) -> Step:
        return Step(
            index=index,
            phase=InvestigationPhase.REFLECTION,
            moved_to=InvestigationPhase.COLLECTION,
            reason="placeholder",
        )

    @classmethod
    def strong_appraisal(cls, credibility: InformationCredibility) -> AppraisalResult:
        return AppraisalResult(
            assertions=(
                ExtractedAssertion(
                    assertion="Acme filed accounts for 2024.",
                    document_url=cls.ARTICLE,
                    credibility=credibility,
                ),
            ),
            gradings=(
                SourceGrading(
                    domain="reuters.com",
                    reliability=SourceReliability.COMPLETELY_RELIABLE,
                    reason="major wire service",
                ),
            ),
        )

    @classmethod
    def directed(cls) -> ScriptedModel:
        model = ScriptedModel()
        model.script("direction", DirectionPlan(hypotheses=("Acme operates.", "Acme is dormant.")))
        return model


class StubSource(Tool):
    """A source that returns fixed documents, without touching the network."""

    def __init__(self, documents: tuple[Document, ...], cassette: Cassette | None = None) -> None:
        super().__init__(cassette if cassette is not None else Cassette(mode=CassetteMode.RECORD))
        self._documents = documents

    @property
    def name(self) -> str:
        return "stub_source"

    def retrieve(self, _query: str) -> tuple[Document, ...]:
        return self._documents


class DeadSource(Tool):
    """A source that is unreachable, so failure handling can be exercised."""

    @property
    def name(self) -> str:
        return "dead_source"

    def retrieve(self, _query: str) -> tuple[Document, ...]:
        raise httpx.ConnectError("host unreachable")


class TestDirection:
    def test_refuses_to_proceed_on_a_single_hypothesis(self) -> None:
        model = ScriptedModel()
        model.script("direction", DirectionPlan(hypotheses=("Acme is fine.",)))
        investigation = Episode.opened()

        Direction(model).advance(investigation)

        assert len(investigation.hypotheses) >= Direction.MINIMUM_HYPOTHESES

    def test_keeps_the_analyst_hypotheses_when_enough_are_offered(self) -> None:
        model = ScriptedModel()
        model.script("direction", DirectionPlan(hypotheses=("First.", "Second.", "Third.")))
        investigation = Episode.opened()

        Direction(model).advance(investigation)

        assert [h.statement for h in investigation.hypotheses] == ["First.", "Second.", "Third."]

    def test_falls_back_to_the_subjects_own_questions_when_none_are_proposed(self) -> None:
        model = ScriptedModel()
        model.script("direction", DirectionPlan())
        investigation = Episode.opened()

        Direction(model).advance(investigation)

        assert investigation.leads

    def test_moves_to_collection_and_meters_its_own_cost(self) -> None:
        model = ScriptedModel()
        model.script("direction", DirectionPlan(hypotheses=("a", "b")))
        investigation = Episode.opened()

        transition = Direction(model).advance(investigation)

        assert transition.next_phase is InvestigationPhase.COLLECTION
        assert transition.input_tokens > 0


class TestCollection:
    def _planned(self) -> ScriptedModel:
        model = ScriptedModel()
        model.script("collection", CollectionPlan(encyclopedia_lookups=("Acme Corp",)))
        return model

    def test_records_retrieved_documents_against_the_investigation(self) -> None:
        investigation = Episode.opened()

        Collection(
            self._planned(), StubSource((Episode.document(),)), StubSource(()), StubSource(())
        ).advance(investigation)

        assert Episode.ARTICLE in investigation.documents

    def test_an_unreachable_source_is_recorded_not_fatal(self) -> None:
        investigation = Episode.opened()
        dead = DeadSource(Cassette(mode=CassetteMode.RECORD))

        transition = Collection(
            self._planned(), dead, StubSource(()), StubSource(())
        ).advance(investigation)

        assert transition.next_phase is InvestigationPhase.APPRAISAL
        assert transition.tool_calls[0].succeeded is False
        assert "unreachable" in transition.tool_calls[0].failure_reason

    def test_a_replay_miss_is_recorded_rather_than_ending_the_episode(self) -> None:
        investigation = Episode.opened()
        sealed = StubSource((), cassette=Cassette(mode=CassetteMode.REPLAY))

        transition = Collection(
            self._planned(), sealed, StubSource(()), StubSource(())
        ).advance(investigation)

        assert transition.tool_calls[0].succeeded is False
        assert "cassette" in transition.tool_calls[0].failure_reason

    def test_open_leads_are_marked_pursued(self) -> None:
        investigation = Episode.opened()
        investigation.leads.append(Lead(question="Who owns it?"))

        Collection(
            self._planned(), StubSource((Episode.document(),)), StubSource(()), StubSource(())
        ).advance(investigation)

        assert investigation.open_leads() == ()

    def test_a_chosen_search_hit_is_opened_in_full_and_recorded(self) -> None:
        investigation = Episode.opened()
        model = ScriptedModel()
        model.script("collection", CollectionPlan(search_queries=("Acme dissolved",)))
        model.script("reading_choice", ReadingChoice(urls=(Episode.ARTICLE,)))
        snippet = Document.retrieved(url=Episode.ARTICLE, title="hit", text="short snippet")

        Collection(
            model, StubSource(()), StubSource((Episode.document(),)), StubSource((snippet,))
        ).advance(investigation)

        assert investigation.documents[Episode.ARTICLE].text == Episode.document().text

    def test_a_search_hit_never_opened_is_not_recorded_as_a_document(self) -> None:
        investigation = Episode.opened()
        model = ScriptedModel()
        model.script("collection", CollectionPlan(search_queries=("Acme dissolved",)))
        model.script("reading_choice", ReadingChoice(urls=()))
        other = "https://ft.com/unopened-hit"
        snippet = Document.retrieved(url=other, title="hit", text="short snippet")

        Collection(
            model, StubSource(()), StubSource(()), StubSource((snippet,))
        ).advance(investigation)

        assert other not in investigation.documents

    def test_a_failing_search_tool_is_recorded_as_a_failed_lookup_not_a_crash(self) -> None:
        investigation = Episode.opened()
        model = ScriptedModel()
        model.script("collection", CollectionPlan(search_queries=("Acme dissolved",)))
        dead = DeadSource(Cassette(mode=CassetteMode.RECORD))

        transition = Collection(
            model, StubSource(()), StubSource(()), dead
        ).advance(investigation)

        failed = [call for call in transition.tool_calls if call.tool == "dead_source"]
        assert failed and failed[0].succeeded is False


class TestAppraisal:
    def test_extracted_assertions_become_graded_evidence(self) -> None:
        model = ScriptedModel()
        model.script("appraisal", Episode.strong_appraisal(InformationCredibility.CONFIRMED))
        investigation = Episode.with_document()

        Appraisal(model).advance(investigation)

        assert len(investigation.evidence) == 1
        graded = investigation.source_for("reuters.com")
        assert graded.reliability is SourceReliability.COMPLETELY_RELIABLE
        assert "wire service" in graded.reason

    def test_an_assertion_citing_an_unretrieved_document_is_refused_and_reported(self) -> None:
        model = ScriptedModel()
        model.script(
            "appraisal",
            AppraisalResult(
                assertions=(
                    ExtractedAssertion(
                        assertion="Invented fact.",
                        document_url="https://never-fetched.example/x",
                        credibility=InformationCredibility.CONFIRMED,
                    ),
                )
            ),
        )
        investigation = Episode.with_document()

        transition = Appraisal(model).advance(investigation)

        assert investigation.evidence == {}
        assert "refused 1" in transition.reason

    def test_nothing_to_read_moves_straight_on(self) -> None:
        transition = Appraisal(ScriptedModel()).advance(Episode.opened())

        assert transition.next_phase is InvestigationPhase.RECONCILIATION
        assert "no new documents" in transition.reason


class TestReconciliation:
    def _evidenced(self, credibility: InformationCredibility) -> Investigation:
        investigation = Episode.with_document()
        Direction(Episode.directed()).advance(investigation)
        model = ScriptedModel()
        model.script("appraisal", Episode.strong_appraisal(credibility))
        Appraisal(model).advance(investigation)
        return investigation

    def test_strong_evidence_lets_a_conclusive_judgment_stand(self) -> None:
        investigation = self._evidenced(InformationCredibility.CONFIRMED)
        evidence_id = next(iter(investigation.evidence))
        model = ScriptedModel()
        model.script(
            "reconciliation",
            ReconciliationResult(
                calls=(
                    ConsistencyCall(
                        hypothesis_index=0,
                        evidence_id=evidence_id,
                        consistency=Consistency.CONSISTENT,
                    ),
                ),
                judgment=Judgment.SUPPORTED,
                probability=0.85,
            ),
        )

        Reconciliation(model).advance(investigation)

        assert investigation.latest_assessment().judgment is Judgment.SUPPORTED

    def test_thin_evidence_is_overridden_however_confident_the_analyst_sounds(self) -> None:
        investigation = self._evidenced(InformationCredibility.IMPROBABLE)
        model = ScriptedModel()
        model.script(
            "reconciliation", ReconciliationResult(judgment=Judgment.SUPPORTED, probability=0.97)
        )

        Reconciliation(model).advance(investigation)

        assert investigation.latest_assessment().judgment is Judgment.INSUFFICIENT_EVIDENCE
        assert investigation.latest_assessment().probability <= 0.5

    def test_calls_referring_to_things_that_do_not_exist_are_discarded(self) -> None:
        """Found live: this test set up exactly the scenario that broke, strong evidence with
        every call discarded, and stopped at checking the mechanical fact (0 applied) without
        checking the property that actually mattered, whether the resulting verdict was trustworthy.
        It was not: `_defensible` only checked evidence weight, so a model's own confident
        `judgment`/`probability` sailed through untouched even with zero real ACH judgments behind
        it, on a real run against a real free model. Both assertions now stand, the second is the
        one that would have caught it."""
        investigation = self._evidenced(InformationCredibility.CONFIRMED)
        model = ScriptedModel()
        model.script(
            "reconciliation",
            ReconciliationResult(
                calls=(
                    ConsistencyCall(
                        hypothesis_index=99,
                        evidence_id="deadbeef1234",
                        consistency=Consistency.INCONSISTENT,
                    ),
                ),
                judgment=Judgment.SUPPORTED,
                probability=0.8,
            ),
        )

        transition = Reconciliation(model).advance(investigation)

        assert "scored 0" in transition.reason
        assert investigation.latest_assessment().judgment is Judgment.INSUFFICIENT_EVIDENCE
        assert investigation.latest_assessment().probability <= 0.5

    def test_an_empty_calls_list_is_the_same_gap_as_calls_that_do_not_exist(self) -> None:
        """The exact shape a live run actually produced: `calls=()` outright rather than calls
        referring to bogus indices, same root cause, same fix, worth its own case since an empty
        tuple and a tuple of unresolvable references are different inputs even if they should
        reach the same verdict."""
        investigation = self._evidenced(InformationCredibility.CONFIRMED)
        model = ScriptedModel()
        model.script(
            "reconciliation",
            ReconciliationResult(calls=(), judgment=Judgment.REFUTED, probability=0.9),
        )

        Reconciliation(model).advance(investigation)

        assert investigation.latest_assessment().judgment is Judgment.INSUFFICIENT_EVIDENCE
        assert investigation.latest_assessment().leading_hypothesis == ""


class TestReflection:
    def _reflecting(self, result: ReflectionResult) -> ScriptedModel:
        model = ScriptedModel()
        model.script("reflection", result)
        return model

    def test_returns_to_collection_while_a_blocking_lead_is_open(self) -> None:
        investigation = Episode.opened()
        investigation.leads.append(Lead(question="Who owns it?", priority=LeadPriority.HIGH))

        transition = Reflection(
            self._reflecting(ReflectionResult(ready_to_conclude=True))
        ).advance(investigation)

        assert transition.next_phase is InvestigationPhase.COLLECTION

    def test_concludes_when_ready_and_nothing_is_blocking(self) -> None:
        transition = Reflection(
            self._reflecting(ReflectionResult(ready_to_conclude=True))
        ).advance(Episode.opened())

        assert transition.next_phase is InvestigationPhase.DISSEMINATION

    def test_new_leads_and_hypotheses_are_adopted(self) -> None:
        investigation = Episode.opened()
        model = self._reflecting(
            ReflectionResult(
                new_leads=(PlannedLead(question="Check the registry."),),
                new_hypotheses=("A third possibility.",),
            )
        )

        Reflection(model).advance(investigation)

        assert investigation.open_leads()[0].origin == "reflection"
        assert investigation.hypotheses[-1].statement == "A third possibility."

    def test_reports_rather_than_running_out_of_budget_silently(self) -> None:
        investigation = Episode.opened(budget=Budget(max_steps=3))
        investigation.leads.append(Lead(question="Still open", priority=LeadPriority.HIGH))
        for index in range(2):
            investigation.record_step(Episode.placeholder_step(index))

        transition = Reflection(
            self._reflecting(ReflectionResult(ready_to_conclude=False))
        ).advance(investigation)

        assert transition.next_phase is InvestigationPhase.DISSEMINATION
        assert "budget" in transition.reason


class TestDissemination:
    def test_writes_the_report_and_records_it(self) -> None:
        investigation = Episode.opened()
        model = ScriptedModel()
        model.script(
            "dissemination",
            ReportDraft(summary="Acme is an operating company.", key_findings=("Filed accounts.",)),
        )

        transition = Dissemination(model).advance(investigation)

        assert transition.next_phase is InvestigationPhase.COMPLETE
        assert investigation.findings.is_written()
        assert investigation.findings.key_findings == ("Filed accounts.",)

    def test_refuses_to_report_on_a_citation_with_no_document_behind_it(self) -> None:
        investigation = Episode.with_document()
        forged = Episode.forged_evidence()
        investigation.evidence[forged.identifier] = forged
        model = ScriptedModel()
        model.script("dissemination", ReportDraft(summary="anything"))

        with pytest.raises(UngroundedEvidenceError, match="refusing to report"):
            Dissemination(model).advance(investigation)


class TestBriefingMemoryModes:
    def _briefed(self, mode: MemoryMode) -> str:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=mode
        )
        investigation.leads.append(Lead(question="Who owns it?"))
        investigation.recalled_priors = (
            Recollection(
                text="Acme was investigated in 2024 and looked clean.",
                from_run="earlier-run",
                about="Acme Corp",
                similarity=0.9,
            ),
        )
        return Briefing(investigation).header()

    def test_none_withholds_the_working_state(self) -> None:
        rendered = self._briefed(MemoryMode.NONE)

        assert "Acme Corp" in rendered
        assert "Who owns it?" not in rendered

    def test_short_carries_the_working_state_but_no_recall(self) -> None:
        rendered = self._briefed(MemoryMode.SHORT)

        assert "Who owns it?" in rendered
        assert "2024 and looked clean" not in rendered

    def test_long_adds_recall_and_labels_it_as_something_that_may_not_be_cited(self) -> None:
        rendered = self._briefed(MemoryMode.LONG)

        assert "2024 and looked clean" in rendered
        assert "NOT evidence" in rendered
        assert "never cite it" in rendered


class TestFullCycle:
    def _wired(self) -> tuple[dict[InvestigationPhase, Node], ScriptedModel]:
        model = ScriptedModel()
        model.script("direction", DirectionPlan(hypotheses=("Acme operates.", "Acme is dormant.")))
        model.script("collection", CollectionPlan(encyclopedia_lookups=("Acme Corp",)))
        model.script("appraisal", Episode.strong_appraisal(InformationCredibility.CONFIRMED))
        # The reconciliation call must actually reference the one piece of evidence appraisal
        # above will extract, or a real defect this project shipped once already recurs: a
        # verdict this test never checked because it was, itself, backed by nothing.
        evidence_id = Evidence(
            assertion="Acme filed accounts for 2024.",
            document_url=Episode.ARTICLE,
            source_domain="reuters.com",
            credibility=InformationCredibility.CONFIRMED,
        ).identifier
        model.script(
            "reconciliation",
            ReconciliationResult(
                calls=(
                    ConsistencyCall(
                        hypothesis_index=0,
                        evidence_id=evidence_id,
                        consistency=Consistency.CONSISTENT,
                    ),
                ),
                judgment=Judgment.SUPPORTED,
                probability=0.82,
            ),
        )
        model.script("reflection", ReflectionResult(ready_to_conclude=True))
        model.script(
            "dissemination",
            ReportDraft(summary="Acme is an operating company.", key_findings=("Filed 2024.",)),
        )
        nodes: dict[InvestigationPhase, Node] = {
            InvestigationPhase.DIRECTION: Direction(model),
            InvestigationPhase.COLLECTION: Collection(
                model, StubSource((Episode.document(),)), StubSource(()), StubSource(())
            ),
            InvestigationPhase.APPRAISAL: Appraisal(model),
            InvestigationPhase.RECONCILIATION: Reconciliation(model),
            InvestigationPhase.REFLECTION: Reflection(model),
            InvestigationPhase.DISSEMINATION: Dissemination(model),
        }
        return nodes, model

    def test_an_investigation_runs_end_to_end_with_no_network_and_no_api_key(self) -> None:
        nodes, _ = self._wired()
        investigation = Episode.opened()

        InvestigationGraph(nodes).run(investigation)

        assert investigation.phase is InvestigationPhase.COMPLETE
        assert investigation.findings.is_written()
        assert investigation.ungrounded_citations() == ()
        assert investigation.latest_assessment().judgment is Judgment.SUPPORTED

    def test_every_phase_is_recorded_with_its_token_cost(self) -> None:
        nodes, model = self._wired()
        investigation = Episode.opened()

        InvestigationGraph(nodes).run(investigation)

        assert [step.phase for step in investigation.steps] == list(
            InvestigationGraph.wired_phases()
        )
        assert investigation.token_cost() == model.spent().total()

    def test_a_claim_investigation_completes_on_the_same_machinery(self) -> None:
        nodes, model = self._wired()
        investigation = Investigation.open(
            run_id="r2",
            subject=Claim(name="acme-revenue", proposition="Acme Corp earned $1B in 2024."),
            memory_mode=MemoryMode.SHORT,
        )

        InvestigationGraph(nodes).run(investigation)

        assert investigation.phase is InvestigationPhase.COMPLETE
        assert model.prompts_seen
