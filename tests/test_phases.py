from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

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


class BlockingPages(Tool):
    """Pages that refuse this reader at some URLs and read normally at every other one."""

    def __init__(self, blocked: tuple[str, ...]) -> None:
        super().__init__(Cassette(mode=CassetteMode.RECORD))
        self._blocked = blocked

    @property
    def name(self) -> str:
        return "blocking_pages"

    def retrieve(self, query: str) -> tuple[Document, ...]:
        if query in self._blocked:
            raise httpx.HTTPError(f"Client error '403 Forbidden' for url '{query}'")
        return (Document.retrieved(url=query, title="a page", text="The page reads normally."),)


class TestDirection:
    def test_refuses_to_proceed_on_a_single_hypothesis(self) -> None:
        model = ScriptedModel()
        model.script("direction", DirectionPlan(hypotheses=("Acme is fine.",)))
        investigation = Episode.opened()

        Direction(model).advance(investigation)

        assert len(investigation.hypotheses) >= 2

    def test_the_subjects_own_hypotheses_are_kept_and_the_analyst_can_only_add(self) -> None:
        """This used to let three offered hypotheses replace the subject's set outright. A live run
        showed the model's own four silently replacing a claim's set, so the hypotheses written to
        cover every verdict are now kept, and at most two genuinely different ones are added."""
        model = ScriptedModel()
        model.script("direction", DirectionPlan(hypotheses=("First.", "Second.", "Third.")))
        investigation = Episode.opened()

        Direction(model).advance(investigation)

        statements = [h.statement for h in investigation.hypotheses]
        assert statements[:3] == list(investigation.subject.opening_hypotheses())
        assert statements[3:] == ["First.", "Second."]

    def test_an_offered_hypothesis_restating_the_subjects_own_is_not_added_twice(self) -> None:
        investigation = Episode.opened()
        restated = f"  {investigation.subject.opening_hypotheses()[1].upper()} "
        model = ScriptedModel()
        model.script("direction", DirectionPlan(hypotheses=(restated,)))

        Direction(model).advance(investigation)

        assert len(investigation.hypotheses) == len(investigation.subject.opening_hypotheses())

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


class TestHypothesisListsRecoverFromAStrayWrappingObject:
    """Found live, through the document-upload path: a model read a hypothesis containing a colon
    as a label needing its own JSON key, and wrapped it in a one-entry object instead of writing
    the plain string the schema asks for. The reply failed validation outright and halted the
    episode at Direction, its very first phase. The prompt no longer writes a hypothesis with a
    colon in it, but both fields that ever hold a freshly-written hypothesis recover the same
    sentence from that shape rather than treat it as unparseable."""

    def test_direction_plan_recovers_a_wrapped_hypothesis(self) -> None:
        plan = DirectionPlan.model_validate(
            {"hypotheses": [{"the assertion is accurate": "Acme Corp operates."}, "A plain one."]}
        )

        assert plan.hypotheses == ("the assertion is accurate: Acme Corp operates.", "A plain one.")

    def test_reflection_result_recovers_a_wrapped_hypothesis_too(self) -> None:
        result = ReflectionResult.model_validate(
            {"new_hypotheses": [{"a third possibility": "Acme was acquired."}]}
        )

        assert result.new_hypotheses == ("a third possibility: Acme was acquired.",)

    def test_a_multi_key_object_is_left_for_the_schema_to_reject(self) -> None:
        """Recovering a one-entry object is a narrow, confident guess at what the model meant.
        Guessing at a multi-key object would be exactly that, a guess, so this is left to fail
        validation normally rather than silently inventing a specific reconstruction."""
        with pytest.raises(ValidationError):
            DirectionPlan.model_validate({"hypotheses": [{"a": "1", "b": "2"}]})


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

    def _reading(self, chosen: tuple[str, ...], proposed: tuple[str, ...] = ()) -> ScriptedModel:
        model = ScriptedModel()
        model.script(
            "collection",
            CollectionPlan(search_queries=("Acme dissolved",), urls_to_read=proposed),
        )
        model.script("reading_choice", ReadingChoice(urls=chosen))
        return model

    def _hit(self) -> StubSource:
        return StubSource((Document.retrieved(url=Episode.ARTICLE, title="hit", text="snippet"),))

    def test_a_page_that_refuses_the_reader_gives_its_slot_to_the_next_candidate(self) -> None:
        """Live runs lost a fifth of all tool calls to pages answering 403, 401 or 404, and each
        refusal used to spend one of the reading slots anyway, so a round could open four pages
        and read none. Reading now goes on down the candidates until enough pages actually read.

        One blocked URL, not two: with two blocked plus this many readable, the outer candidate
        cap (`MAX_READ_ATTEMPTS`) already trims the list to exactly what four successes need, so
        the inner stop-at-four check never has a spare candidate left to prove it skips. One
        blocked candidate leaves one readable candidate spare, so a mutant that deletes the
        stop-at-four check reads it too, and the two assertions below start disagreeing."""
        blocked = tuple(f"https://blocked.example/{n}" for n in range(1))
        readable = tuple(f"https://open.example/{n}" for n in range(5))
        investigation = Episode.opened()

        transition = Collection(
            self._reading(blocked + readable), StubSource(()), BlockingPages(blocked), self._hit()
        ).advance(investigation)

        reads = [call for call in transition.tool_calls if call.tool == "blocking_pages"]
        assert sum(call.succeeded for call in reads) == Collection.MAX_READS
        assert len(reads) == len(blocked) + Collection.MAX_READS
        assert readable[-1] not in [call.query for call in reads]

    def test_reading_stops_once_enough_pages_succeed_even_with_candidates_left(self) -> None:
        """Isolates the stop-at-four check with no failures in the mix: six candidates would all
        succeed if tried, so only the check itself, not a run of refusals, explains two going
        untried."""
        readable = tuple(f"https://open.example/{n}" for n in range(6))
        investigation = Episode.opened()

        transition = Collection(
            self._reading(readable), StubSource(()), BlockingPages(()), self._hit()
        ).advance(investigation)

        reads = [call for call in transition.tool_calls if call.tool == "blocking_pages"]
        assert len(reads) == Collection.MAX_READS
        assert all(call.succeeded for call in reads)

    def test_refused_pages_cannot_run_the_tool_budget_down_unbounded(self) -> None:
        blocked = tuple(f"https://blocked.example/{n}" for n in range(12))
        investigation = Episode.opened()

        transition = Collection(
            self._reading(blocked), StubSource(()), BlockingPages(blocked), self._hit()
        ).advance(investigation)

        reads = [call for call in transition.tool_calls if call.tool == "blocking_pages"]
        assert len(reads) == Collection.MAX_READ_ATTEMPTS

    def test_a_url_the_analyst_already_named_is_opened_before_ones_search_turned_up(self) -> None:
        """Unchanged from before this method started retrying past failures: a URL the analyst
        already had a specific reason to name, such as a citation it already knows about, is not
        one a pile of search hits should be able to crowd out of the reading budget."""
        guessed = "https://named.example/already-known"
        investigation = Episode.opened()

        transition = Collection(
            self._reading((Episode.ARTICLE,), proposed=(guessed,)),
            StubSource(()),
            BlockingPages(()),
            self._hit(),
        ).advance(investigation)

        reads = [call.query for call in transition.tool_calls if call.tool == "blocking_pages"]
        assert reads == [guessed, Episode.ARTICLE]


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

    def test_the_prompt_asks_for_the_author_own_voice_not_a_rebutted_claim(self) -> None:
        model = ScriptedModel()
        model.script("appraisal", Episode.strong_appraisal(InformationCredibility.CONFIRMED))

        Appraisal(model).advance(Episode.with_document())

        assert "own author states to be true" in model.prompts_seen[0]
        assert "never the claim restated bare" in model.prompts_seen[0]

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
                rationale="The filing is confirmed by a reliable source.",
            ),
        )

        Reconciliation(model).advance(investigation)

        assert investigation.latest_assessment().judgment is Judgment.SUPPORTED

    def test_thin_evidence_is_overridden_however_confident_the_analyst_sounds(self) -> None:
        investigation = self._evidenced(InformationCredibility.IMPROBABLE)
        model = ScriptedModel()
        model.script(
            "reconciliation",
            ReconciliationResult(
                judgment=Judgment.SUPPORTED, probability=0.97, rationale="It plainly operates."
            ),
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
                rationale="Everything in the filings checks out.",
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
            ReconciliationResult(
                calls=(),
                judgment=Judgment.REFUTED,
                probability=0.9,
                rationale="The filings contradict it.",
            ),
        )

        Reconciliation(model).advance(investigation)

        assert investigation.latest_assessment().judgment is Judgment.INSUFFICIENT_EVIDENCE
        assert investigation.latest_assessment().leading_hypothesis == ""

    def test_a_reply_that_scores_nothing_and_says_nothing_leaves_the_standing_verdict(
        self,
    ) -> None:
        """Found live: every field of the reply has a default, so an empty object validates, and a
        second-round reply that was exactly that overwrote a correct `supported` at 0.92 with the
        defaults' `insufficient_evidence` at 0.5."""
        investigation = self._evidenced(InformationCredibility.CONFIRMED)
        evidence_id = next(iter(investigation.evidence))
        considered = ScriptedModel()
        considered.script(
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
                probability=0.92,
                rationale="Filings corroborate the description.",
            ),
        )
        Reconciliation(considered).advance(investigation)
        silent = ScriptedModel()
        silent.script("reconciliation", ReconciliationResult())

        transition = Reconciliation(silent).advance(investigation)

        assert investigation.latest_assessment().judgment is Judgment.SUPPORTED
        assert investigation.latest_assessment().probability == 0.92
        assert "standing assessment and ACH matrix stand" in transition.reason

    def test_real_calls_with_no_rationale_do_not_touch_the_matrix_either(self) -> None:
        """Found live, a second bug in the same shape: a reply carrying a real, resolvable ACH
        matrix (`applied > 0`) reversed a well-reasoned `partially_supported` to `refuted` with an
        empty rationale. Refusing only the verdict and not the matrix would have left the dossier
        contradicting itself: the standing verdict kept, but the hypothesis table showing the
        opposite hypothesis as the one every piece of evidence now confirmed."""
        investigation = self._evidenced(InformationCredibility.CONFIRMED)
        evidence_id = next(iter(investigation.evidence))
        considered = ScriptedModel()
        considered.script(
            "reconciliation",
            ReconciliationResult(
                calls=(
                    ConsistencyCall(
                        hypothesis_index=1,
                        evidence_id=evidence_id,
                        consistency=Consistency.INCONSISTENT,
                    ),
                ),
                judgment=Judgment.PARTIALLY_SUPPORTED,
                probability=0.9,
                rationale="Half of it holds up; the other half does not.",
            ),
        )
        before = Reconciliation(considered).advance(investigation)
        matrix_before = dict(investigation.hypotheses[1].consistency)
        silent = ScriptedModel()
        silent.script(
            "reconciliation",
            ReconciliationResult(
                calls=(
                    ConsistencyCall(
                        hypothesis_index=1,
                        evidence_id=evidence_id,
                        consistency=Consistency.CONSISTENT,
                    ),
                ),
                judgment=Judgment.REFUTED,
                probability=0.95,
            ),
        )

        Reconciliation(silent).advance(investigation)

        assert investigation.latest_assessment().judgment is Judgment.PARTIALLY_SUPPORTED
        assert investigation.hypotheses[1].consistency == matrix_before
        assert before  # the first, reasoned transition is exercised above


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

    def test_every_mode_says_what_the_verdict_is_a_verdict_on(self) -> None:
        for mode in MemoryMode:
            rendered = self._briefed(mode)

            assert "PROPOSITION UNDER TEST: Acme Corp is a real entity" in rendered
            assert "- insufficient_evidence: " in rendered


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
                rationale="The filing is confirmed by a reliable source.",
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

    def test_a_reflection_that_never_concludes_still_ends_in_a_report(self) -> None:
        """Three of the last four live runs halted without a report. Reflection held back two
        steps for it, but the round it sent the investigation back for costs four, so under a
        twelve-step ceiling the third round always ran the budget out before the report."""
        nodes, model = self._wired()
        model.script("reflection", ReflectionResult(ready_to_conclude=False))
        investigation = Episode.opened(budget=Budget(max_steps=12))

        InvestigationGraph(nodes).run(investigation)

        assert investigation.phase is InvestigationPhase.COMPLETE
        assert investigation.findings.is_written()
