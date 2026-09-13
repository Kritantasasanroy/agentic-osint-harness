"""Regression tests, one per finding from the independent gate audit.

Each test names the defect it exists to prevent. They are kept together rather than scattered so
that the cost of the audit is visible, and so a future change that quietly reintroduces one of
these fails against a test that says plainly what was wrong the first time.
"""

from datetime import UTC, datetime

import httpx
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
    Source,
    SourceReliability,
)
from osint_harness.domain.subject import Company
from osint_harness.graph.briefing import Briefing
from osint_harness.graph.phases import Appraisal, Collection, Dissemination, Reconciliation
from osint_harness.graph.schemas import (
    AppraisalResult,
    CollectionPlan,
    ExtractedAssertion,
    ReconciliationResult,
    ReportDraft,
    SourceGrading,
)
from osint_harness.model.client import ScriptedModel
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


class DeadWebSearch(Tool):
    """A search tool that cannot be reached, to prove the failure is recorded, not swallowed."""

    @property
    def name(self) -> str:
        return "web_search"

    def retrieve(self, _query: str) -> tuple[Document, ...]:
        raise httpx.ConnectError("search backend unavailable")


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
    """Gate finding (predating the OpenRouter/tool-based search rewrite): the old model-driven
    search hard-coded `succeeded=True` and caught nothing, so a failing search either crashed the
    episode or was logged as a success and could never raise TOOL_FAILURE. Search later became a
    `Tool` like every other source; this proves the same property still holds under that
    architecture — a failing search is a recorded failure, never a crash or a false success."""

    def test_a_failing_search_is_recorded_as_a_failed_lookup(self) -> None:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        model = ScriptedModel()
        model.script("collection", CollectionPlan(search_queries=("Acme Corp",)))
        empty = DeadTool(Cassette(mode=CassetteMode.RECORD))
        dead_search = DeadWebSearch(Cassette(mode=CassetteMode.RECORD))

        transition = Collection(model, empty, empty, dead_search).advance(investigation)

        searches = [call for call in transition.tool_calls if call.tool == "web_search"]
        assert searches
        assert searches[0].succeeded is False
        assert "unavailable" in searches[0].failure_reason


class TestAGradingSurvivesWhateverTheModelWritesInDomain:
    """The original bug was found by actually running the live path, not by an audit: a real free
    model graded sources thoughtfully but wrote the domain field as "en.wikipedia.org —
    biographical and technical articles" rather than a bare hostname. Because evidence is always
    keyed by the clean domain Source.registrable_domain derives from its URL, the grading landed
    under a different dictionary key than anything ever looked up — real analytic work, silently
    discarded.

    Two subsequent independent-audit rounds each broke the fix that closed the round before it —
    a token-splitting version broken by trailing punctuation and a bare URL, then a
    punctuation-stripping-plus-URL-detection version broken by a domain sitting mid-sentence and a
    scheme-less URL with a path. The current version searches for the hostname *pattern* itself
    (dot-separated labels, wherever they occur) rather than guessing at the domain's position in
    the string, which is what both earlier versions were actually doing under different framing."""

    def test_a_domain_with_trailing_commentary_still_grades_its_evidence(self) -> None:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        investigation.record_document(Episode.document())
        model = ScriptedModel()
        model.script(
            "appraisal",
            AppraisalResult(
                assertions=(
                    ExtractedAssertion(
                        assertion=Episode.ASSERTION,
                        document_url=Episode.ARTICLE,
                        credibility=InformationCredibility.CONFIRMED,
                    ),
                ),
                gradings=(
                    SourceGrading(
                        domain="reuters.com — a major international wire service",
                        reliability=SourceReliability.COMPLETELY_RELIABLE,
                        reason="wire service",
                    ),
                ),
            ),
        )

        Appraisal(model).advance(investigation)

        assert investigation.reliability_of("reuters.com") is SourceReliability.COMPLETELY_RELIABLE

    def test_domain_only_keeps_a_clean_domain_unchanged(self) -> None:
        assert Source.domain_only("reuters.com") == "reuters.com"

    def test_domain_only_finds_a_domain_wrapped_in_commentary_and_normalises_it(self) -> None:
        assert Source.domain_only("Reuters.com — a wire service") == "reuters.com"
        assert Source.domain_only("www.reuters.com and nothing else") == "reuters.com"

    def test_domain_only_is_not_thrown_by_trailing_punctuation(self) -> None:
        """Round one of an independent audit: a token followed directly by punctuation, with no
        separating whitespace, kept the punctuation under the first (token-splitting) version of
        this fix and so still failed to match the clean key evidence is stored under."""
        assert Source.domain_only("en.wikipedia.org, and other pages") == "en.wikipedia.org"
        assert Source.domain_only("reuters.com.") == "reuters.com"

    def test_domain_only_reduces_a_full_url_to_its_host(self) -> None:
        """Round one, counterexample two: a model echoing back a full URL instead of a bare
        domain, left completely unreduced by the first version."""
        assert (
            Source.domain_only("https://en.wikipedia.org/wiki/Ada_Lovelace") == "en.wikipedia.org"
        )
        assert Source.domain_only("//en.wikipedia.org/wiki/Ada_Lovelace") == "en.wikipedia.org"

    def test_domain_only_finds_the_domain_regardless_of_where_it_sits_in_the_sentence(
        self,
    ) -> None:
        """Round two of the same audit, against the token-splitting-plus-punctuation-stripping
        second version: a domain preceded by other words was never found at all, because that
        version only ever looked at the FIRST token. `"the domain is en.wikipedia.org"` returned
        `"the"` — a plausible model phrasing this project had not yet tried to break."""
        assert Source.domain_only("the domain is en.wikipedia.org") == "en.wikipedia.org"

    def test_domain_only_reduces_a_schemeless_url_with_a_path_to_its_host(self) -> None:
        """Round two, counterexample two: a URL missing its scheme but still carrying a path
        (`"en.wikipedia.org/wiki/Ada_Lovelace"`, no `http://`) was not recognised as URL-shaped by
        the second version's `"://"`-or-`"//"`-prefix check, so the path stayed attached. The
        current version searches for the hostname pattern itself rather than guessing from a
        scheme marker, so a scheme is no longer required to find it."""
        assert Source.domain_only("en.wikipedia.org/wiki/Ada_Lovelace") == "en.wikipedia.org"
        assert (
            Source.domain_only("mathshistory.st-andrews.ac.uk/Biographies/Lovelace")
            == "mathshistory.st-andrews.ac.uk"
        )

    def test_domain_only_strips_quote_or_backtick_wrapping(self) -> None:
        """The independent audit's own re-verification of round one's fix (the
        punctuation-stripping-plus-URL-detection version) found this could still be broken: neither
        a backtick nor a plain double quote was in that version's trailing-punctuation set, so a
        model wrapping its answer in either, a common way to set off an identifier in generated
        text, left the wrapping characters attached to a key evidence was never stored under. The
        pattern-search version fixes this for the same reason it fixes every other wrapping: a
        quote or backtick isn't hostname-shaped, so the search simply never includes it in the
        match."""
        assert Source.domain_only('"en.wikipedia.org"') == "en.wikipedia.org"
        assert Source.domain_only("`en.wikipedia.org`") == "en.wikipedia.org"

    def test_domain_only_never_returns_empty(self) -> None:
        """Every caller of this keys a `min_length=1` field with the result. Text that strips to
        nothing (all whitespace) used to return an empty string right past that guarantee; found
        live, the hard way, not by an audit this time, see the class below."""
        assert Source.domain_only("   ") == "unknown"
        assert Source.domain_only("") == "unknown"

    def test_a_malformed_assertion_url_still_grades_its_evidence(self) -> None:
        """Found by actually running the live path a second time: the model wrote a real,
        well-formed assertion, but wrote its citation as a scheme-less domain-plus-path instead of
        the exact retrieved URL it was quoting. `Source.registrable_domain`, used to derive the
        evidence's `source_domain` at the time, has no fallback for text that is not already a
        parseable absolute URL, so `urlparse(...).netloc` came back empty and construction of the
        `Evidence` itself raised, crashing the whole investigation instead of just being refused as
        an ungrounded citation, which is what a citation not matching a retrieved document should
        do. Fixed by deriving `source_domain` with `Source.domain_only`, the same tool already used
        for the grading domain field above, since this is the identical failure class: free text a
        model wrote, not a URL this code fetched itself."""
        investigation = Investigation.open(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )
        investigation.record_document(Episode.document())
        model = ScriptedModel()
        model.script(
            "appraisal",
            AppraisalResult(
                assertions=(
                    ExtractedAssertion(
                        assertion=Episode.ASSERTION,
                        document_url="reuters.com/a",
                        credibility=InformationCredibility.CONFIRMED,
                    ),
                ),
                gradings=(),
            ),
        )

        transition = Appraisal(model).advance(investigation)

        assert "refused 1 citing documents that were never retrieved" in transition.reason
