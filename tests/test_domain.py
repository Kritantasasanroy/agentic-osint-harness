from itertools import pairwise

import pytest

from osint_harness.domain.analysis import (
    Assessment,
    ConfidenceBand,
    Consistency,
    Evidence,
    Hypothesis,
    Judgment,
)
from osint_harness.domain.investigation import (
    Budget,
    Investigation,
    InvestigationPhase,
    Lead,
    LeadPriority,
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
from osint_harness.domain.subject import Claim, Company, Person


class TestAdmiraltyGrading:
    def test_reliability_outranks_credibility_independently(self) -> None:
        strong_source_weak_item = SourceReliability.COMPLETELY_RELIABLE.weight() * (
            InformationCredibility.DOUBTFUL.weight()
        )
        weak_source_strong_item = SourceReliability.NOT_USUALLY_RELIABLE.weight() * (
            InformationCredibility.CONFIRMED.weight()
        )
        assert strong_source_weak_item == pytest.approx(weak_source_strong_item)

    def test_unjudgeable_grades_are_discounted_not_zeroed(self) -> None:
        assert 0.0 < SourceReliability.CANNOT_BE_JUDGED.weight() < 0.2
        assert 0.0 < InformationCredibility.CANNOT_BE_JUDGED.weight() < 0.2


class TestConfidenceBand:
    def test_every_band_range_is_ordered_and_within_the_unit_interval(self) -> None:
        for band in ConfidenceBand:
            low, high = band.probability_range()
            assert 0.0 < low < high < 1.0

    def test_probability_maps_to_the_band_containing_it(self) -> None:
        assert ConfidenceBand.for_probability(0.5) is ConfidenceBand.ROUGHLY_EVEN_CHANCE
        assert ConfidenceBand.for_probability(0.7) is ConfidenceBand.LIKELY
        assert ConfidenceBand.for_probability(0.02) is ConfidenceBand.ALMOST_NO_CHANCE

    def test_certainty_saturates_rather_than_falling_off_the_scale(self) -> None:
        assert ConfidenceBand.for_probability(1.0) is ConfidenceBand.ALMOST_CERTAIN

    def test_bands_tile_the_scale_without_gap_or_overlap(self) -> None:
        bands = tuple(ConfidenceBand)
        assert len(ConfidenceBand.boundaries()) == len(bands) + 1
        for lower, upper in pairwise(bands):
            assert lower.probability_range()[1] == upper.probability_range()[0]


class TestEvidence:
    def test_identifier_is_stable_across_identical_assertions(self) -> None:
        first = Evidence(
            assertion="Founded in 2011.",
            document_url="https://example.com/a",
            source_domain="example.com",
            credibility=InformationCredibility.PROBABLY_TRUE,
        )
        second = Evidence(
            assertion="Founded in 2011.",
            document_url="https://example.com/a",
            source_domain="example.com",
            credibility=InformationCredibility.DOUBTFUL,
        )
        assert first.identifier == second.identifier

    def test_identifier_separates_the_same_assertion_from_different_documents(self) -> None:
        first = Evidence(
            assertion="Founded in 2011.",
            document_url="https://example.com/a",
            source_domain="example.com",
            credibility=InformationCredibility.PROBABLY_TRUE,
        )
        second = first.model_copy(update={"document_url": "https://other.org/b"})
        assert first.identifier != second.identifier


class TestAchRanking:
    def test_least_disconfirmed_hypothesis_wins_even_with_less_support(self) -> None:
        weights = {"e1": 1.0, "e2": 1.0, "e3": 1.0}
        well_supported_but_contradicted = Hypothesis(
            statement="Widely reported and also contradicted.",
            consistency={
                "e1": Consistency.CONSISTENT,
                "e2": Consistency.CONSISTENT,
                "e3": Consistency.INCONSISTENT,
            },
        )
        modestly_supported_but_unchallenged = Hypothesis(
            statement="Thinly reported but never contradicted.",
            consistency={"e1": Consistency.CONSISTENT, "e3": Consistency.NOT_APPLICABLE},
        )
        assert well_supported_but_contradicted.support_score(weights) > (
            modestly_supported_but_unchallenged.support_score(weights)
        )
        assert well_supported_but_contradicted.inconsistency_score(weights) > (
            modestly_supported_but_unchallenged.inconsistency_score(weights)
        )

    def test_not_applicable_evidence_is_not_diagnostic(self) -> None:
        hypothesis = Hypothesis(
            statement="Something falsifiable.",
            consistency={"e1": Consistency.CONSISTENT, "e2": Consistency.NOT_APPLICABLE},
        )
        assert hypothesis.diagnostic_evidence() == ("e1",)


class TestAssessment:
    def test_brier_rewards_confident_correctness_and_punishes_confident_error(self) -> None:
        confident = Assessment(
            judgment=Judgment.SUPPORTED, leading_hypothesis="h", probability=0.95
        )
        assert confident.brier_score(was_correct=True) < 0.01
        assert confident.brier_score(was_correct=False) > 0.9

    def test_hedged_assessment_is_never_badly_scored_either_way(self) -> None:
        hedged = Assessment(
            judgment=Judgment.INSUFFICIENT_EVIDENCE, leading_hypothesis="h", probability=0.5
        )
        assert hedged.brier_score(was_correct=True) == pytest.approx(0.25)
        assert hedged.brier_score(was_correct=False) == pytest.approx(0.25)


class TestSubjectPolymorphism:
    def test_each_subject_kind_opens_with_at_least_two_competing_hypotheses(self) -> None:
        subjects = [
            Company(name="Acme Corp", jurisdiction="DE"),
            Person(name="Jane Doe", affiliation="Acme Corp"),
            Claim(name="acme-revenue", proposition="Acme Corp earned $1B in 2024."),
        ]
        for subject in subjects:
            assert len(subject.opening_hypotheses()) >= 2
            assert len(subject.seed_leads()) >= 2

    def test_person_leads_with_disambiguation_because_same_name_is_the_failure_mode(self) -> None:
        person = Person(name="John Smith")
        assert "distinguishes them" in person.seed_leads()[0]

    def test_finding_much_about_one_bearer_of_a_name_does_not_settle_which_one_is_meant(
        self,
    ) -> None:
        """Two live rounds gave John Smith a correct `insufficient_evidence`, then a third,
        running to completion for the first time rather than halting partway, retrieved an
        abundant, internally consistent record for the historical Captain John Smith and reported
        `supported` at 0.85: 'sources describe the same historical figure... supporting the
        existence of a single identifiable individual.' The old wording asked whether the record
        'conflates' distinct people, a property of how well an investigation goes, not of whether
        the query itself picked anyone out; a query with no qualifiers can never pick anyone out,
        no matter how clean the record for whichever bearer Collection happens to retrieve."""
        ambiguous = Person(name="John Smith").opening_hypotheses()[1]

        assert "does not by itself establish" in ambiguous
        assert "extensive" in ambiguous or "abundant" in ambiguous

    def test_descriptor_folds_qualifiers_into_one_searchable_phrase(self) -> None:
        company = Company(name="Acme", qualifiers=("Delaware", "software"))
        assert company.descriptor() == "Acme (Delaware, software)"

    def test_a_verdict_is_always_about_a_stated_proposition(self) -> None:
        claim = Claim(name="acme-revenue", proposition="Acme Corp earned $1B in 2024.")
        named = Person(name="Jane Doe", affiliation="Acme Corp")
        qualified = Person(name="Jane Doe", qualifiers=("Acme Corp",), affiliation="Acme Corp")

        assert claim.proposition_under_test() == "Acme Corp earned $1B in 2024."
        assert "associated with Acme Corp" in named.proposition_under_test()
        assert qualified.proposition_under_test().count("Acme Corp") == 1

    def test_claim_hypotheses_separate_the_core_event_from_an_attached_detail(self) -> None:
        """Two live rounds on einstein-nobel-relativity kept scoring 'inaccurate as stated' and
        'partly accurate but misleading' as equally consistent with the same evidence and then
        reporting refuted, because both were about the assertion 'as stated' as a whole rather than
        about whether the core event happened. A compound assertion ("X did A for reason B") is
        trivially inaccurate as a whole the moment any part is wrong, which makes that hypothesis
        never actually distinct from a partial one worded the same way. Wording both around the
        core event instead of the assertion as a whole keeps them apart. A further live round then
        had 'wholly false' absorb a false-premise case (the King of France) that had been correct
        twice before, since 'does not hold at all' reads just as naturally as 'the premise itself
        is unreal'. Both wholly-false and partly-true now explicitly presuppose a real premise, so
        an unreal one routes to its own hypothesis instead. Even with all of that, one more live
        round still called the Einstein case 'refuted', reasoning that the prize being for the
        photoelectric effect meant 'the core claim... did not occur' outright, still treating the
        reason clause as inseparable from the event it was attached to. Naming the exact clause to
        set aside and stating what is left over as a mechanical step, rather than leaving 'core
        event' for the model to identify unaided, is the next attempt."""
        claim = Claim(name="award-claim", proposition="X received an award for reason Y.")
        wholly_wrong, partly_wrong, false_premise = claim.opening_hypotheses()[1:4]

        assert "presupposes is real" in wholly_wrong
        assert "setting aside any clause" in wholly_wrong
        assert "did not happen or does not hold" in wholly_wrong
        assert "presupposes is real" in partly_wrong
        assert "setting aside any clause" in partly_wrong
        assert "did happen or does hold" in partly_wrong
        assert "set-aside clause is wrong" in partly_wrong
        assert "presupposes something that is not real" in false_premise

    def test_claim_hypotheses_never_end_with_a_colon_then_the_proposition(self) -> None:
        """Found live, through the document-upload path rather than the benchmark: a model read a
        hypothesis ending "...as put: {proposition}" as a label needing its own JSON key, and
        wrapped every hypothesis in a one-entry object instead of writing the plain string the
        schema asks for, which failed validation and halted the episode at the very first phase.
        The proposition is still named in each hypothesis, for the same de-duplication reason as
        before, just woven into the sentence rather than appended after a colon."""
        proposition = "X received an award for reason Y."
        claim = Claim(name="award-claim", proposition=proposition)

        for hypothesis in claim.opening_hypotheses():
            assert not hypothesis.rstrip().endswith(f": {proposition}")
            assert proposition in hypothesis


class TestInvestigation:
    def _open(self) -> Investigation:
        return Investigation.open(
            run_id="r1",
            subject=Company(name="Acme Corp"),
            memory_mode=MemoryMode.SHORT,
        )

    def _record(self, investigation: Investigation, url: str, assertion: str) -> str:
        investigation.record_document(Document.retrieved(url=url, title="t", text="body"))
        investigation.grade_source(
            Document.retrieved(url=url, title="t", text="b").source_domain,
            SourceReliability.USUALLY_RELIABLE,
            "wire service",
        )
        return investigation.record_evidence(
            Evidence(
                assertion=assertion,
                document_url=url,
                source_domain=Document.retrieved(url=url, title="t", text="b").source_domain,
                credibility=InformationCredibility.PROBABLY_TRUE,
            )
        )

    def test_opens_with_a_baseline_assessment_so_progression_is_never_empty(self) -> None:
        investigation = self._open()
        assert investigation.latest_assessment().judgment is Judgment.INSUFFICIENT_EVIDENCE
        assert investigation.confidence_series() == (0.5,)

    def test_evidence_citing_an_unretrieved_document_is_refused(self) -> None:
        investigation = self._open()
        with pytest.raises(UngroundedEvidenceError):
            investigation.record_evidence(
                Evidence(
                    assertion="Invented fact.",
                    document_url="https://never-fetched.example/x",
                    source_domain="never-fetched.example",
                    credibility=InformationCredibility.CONFIRMED,
                )
            )

    def test_grounded_evidence_is_accepted_and_leaves_no_ungrounded_citations(self) -> None:
        investigation = self._open()
        self._record(investigation, "https://reuters.com/a", "Acme filed accounts in 2024.")
        assert investigation.ungrounded_citations() == ()
        assert investigation.source_diversity() == 1

    def test_evidence_weight_combines_the_source_grade_applied_at_retrieval(self) -> None:
        investigation = self._open()
        identifier = self._record(investigation, "https://reuters.com/a", "Acme exists.")
        expected = SourceReliability.USUALLY_RELIABLE.weight() * (
            InformationCredibility.PROBABLY_TRUE.weight()
        )
        assert investigation.evidence_weights()[identifier] == pytest.approx(expected)

    def test_verdict_changes_counts_flips_not_assessments(self) -> None:
        investigation = self._open()
        for judgment in (Judgment.SUPPORTED, Judgment.SUPPORTED, Judgment.REFUTED):
            investigation.assess(
                Assessment(judgment=judgment, leading_hypothesis="h", probability=0.6)
            )
        assert investigation.verdict_changes() == 2

    def test_stability_is_measured_from_the_last_flip_not_the_first_appearance(self) -> None:
        investigation = self._open()
        for judgment in (Judgment.SUPPORTED, Judgment.REFUTED, Judgment.SUPPORTED):
            investigation.assess(
                Assessment(judgment=judgment, leading_hypothesis="h", probability=0.6)
            )
        assert investigation.assessments_to_stable_verdict() == 3

    def test_high_priority_open_leads_block_a_conclusion(self) -> None:
        investigation = self._open()
        investigation.leads.append(Lead(question="Who owns it?", priority=LeadPriority.HIGH))
        assert investigation.has_blocking_leads()
        investigation.leads[0].exhaust()
        assert not investigation.has_blocking_leads()

    def test_ranked_hypotheses_put_the_least_disconfirmed_first(self) -> None:
        investigation = self._open()
        identifier = self._record(investigation, "https://reuters.com/a", "Acme is dissolved.")
        contradicted = Hypothesis(
            statement="Acme is operating.", consistency={identifier: Consistency.INCONSISTENT}
        )
        unchallenged = Hypothesis(
            statement="Acme is dissolved.", consistency={identifier: Consistency.CONSISTENT}
        )
        investigation.hypotheses.extend([contradicted, unchallenged])
        assert investigation.ranked_hypotheses()[0] is unchallenged


class TestBudget:
    def test_step_ceiling_exhausts_the_budget(self) -> None:
        investigation = Investigation.open(
            run_id="r2",
            subject=Company(name="Acme"),
            memory_mode=MemoryMode.NONE,
            budget=Budget(max_steps=1),
        )
        assert not investigation.budget_exhausted()
        investigation.record_step(
            Step(
                index=0,
                phase=InvestigationPhase.DIRECTION,
                moved_to=InvestigationPhase.COLLECTION,
                reason="seeded leads",
            )
        )
        assert investigation.budget_exhausted()


class TestSource:
    def test_registrable_domain_strips_scheme_port_and_www(self) -> None:
        assert Source.registrable_domain("https://www.Reuters.com:443/article/1") == "reuters.com"

    def test_an_unseen_source_starts_unjudgeable_rather_than_trusted(self) -> None:
        assert Source(domain="example.com").reliability is SourceReliability.CANNOT_BE_JUDGED

    def test_regrade_records_both_the_grade_and_its_reason(self) -> None:
        source = Source(domain="example.com")
        source.regrade(SourceReliability.USUALLY_RELIABLE, "major wire service")
        assert source.reliability is SourceReliability.USUALLY_RELIABLE
        assert "wire service" in source.reason


class TestMemoryMode:
    def test_none_carries_nothing_forward(self) -> None:
        assert not MemoryMode.NONE.carries_working_state()
        assert not MemoryMode.NONE.recalls_past_episodes()

    def test_short_carries_the_episode_but_not_the_archive(self) -> None:
        assert MemoryMode.SHORT.carries_working_state()
        assert not MemoryMode.SHORT.recalls_past_episodes()

    def test_long_carries_both(self) -> None:
        assert MemoryMode.LONG.carries_working_state()
        assert MemoryMode.LONG.recalls_past_episodes()
