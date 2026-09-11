from datetime import UTC, datetime

from osint_harness.bench.case import BenchmarkCase, CaseTrap, DefensibleConfidence
from osint_harness.bench.outcome import BenchmarkRun, CaseOutcome, Diagnosis, FailureMode
from osint_harness.bench.reward import EpisodeReward, StepReward
from osint_harness.domain.analysis import Assessment, Evidence, Findings, Judgment
from osint_harness.domain.investigation import (
    Budget,
    Investigation,
    InvestigationPhase,
    Lead,
    MemoryMode,
    Recollection,
    Step,
    ToolCall,
)
from osint_harness.domain.provenance import (
    Document,
    InformationCredibility,
    SourceReliability,
)
from osint_harness.domain.subject import Company


class Scenario:
    """Builds finished investigations in specific states, so scoring can be tested precisely."""

    ARTICLE = "https://reuters.com/a"
    OTHER = "https://ft.com/b"

    @classmethod
    def case(
        cls,
        expected: Judgment = Judgment.SUPPORTED,
        lowest: float = 0.6,
        highest: float = 0.9,
        trap: CaseTrap = CaseTrap.NONE,
    ) -> BenchmarkCase:
        return BenchmarkCase(
            case_id="c1",
            subject=Company(name="Acme Corp"),
            expected=expected,
            confidence=DefensibleConfidence(lowest=lowest, highest=highest),
            trap=trap,
        )

    @classmethod
    def investigation(
        cls,
        judgment: Judgment = Judgment.SUPPORTED,
        probability: float = 0.8,
        subject_name: str = "Acme Corp",
    ) -> Investigation:
        investigation = Investigation.open(
            run_id="r1", subject=Company(name=subject_name), memory_mode=MemoryMode.SHORT
        )
        investigation.assess(
            Assessment(judgment=judgment, leading_hypothesis="h", probability=probability)
        )
        investigation.record_findings(Findings(summary="done", key_findings=("a finding",)))
        return investigation

    @classmethod
    def with_evidence(
        cls,
        investigation: Investigation,
        url: str = ARTICLE,
        grade: SourceReliability = SourceReliability.COMPLETELY_RELIABLE,
        credibility: InformationCredibility = InformationCredibility.CONFIRMED,
        assertion: str = "Acme filed accounts.",
    ) -> str:
        document = Document.retrieved(
            url=url, title="t", text="body", retrieved_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        investigation.record_document(document, grade)
        return investigation.record_evidence(
            Evidence(
                assertion=assertion,
                document_url=url,
                source_domain=document.source_domain,
                credibility=credibility,
            )
        )

    @classmethod
    def step(
        cls,
        index: int = 0,
        evidence: tuple[str, ...] = (),
        tokens: int = 1000,
        calls: tuple[ToolCall, ...] = (),
        phase: InvestigationPhase = InvestigationPhase.APPRAISAL,
    ) -> Step:
        return Step(
            index=index,
            phase=phase,
            moved_to=InvestigationPhase.RECONCILIATION,
            reason="did work",
            evidence_added=evidence,
            tool_calls=calls,
            input_tokens=tokens,
            output_tokens=0,
        )


class TestStepReward:
    def test_credits_new_evidence_by_its_admiralty_weight(self) -> None:
        reward = StepReward.earned(Scenario.step(evidence=("e1",)), {"e1": 0.8})

        assert reward.information_gain == 0.8

    def test_evidence_already_counted_elsewhere_earns_nothing_here(self) -> None:
        reward = StepReward.earned(Scenario.step(evidence=("unknown",)), {"e1": 0.8})

        assert reward.information_gain == 0.0

    def test_retrieval_is_credited_far_more_lightly_than_extraction(self) -> None:
        call = ToolCall(tool="t", query="q", documents_returned=3, succeeded=True)
        retrieval = StepReward.earned(Scenario.step(calls=(call,), tokens=0), {})
        extraction = StepReward.earned(Scenario.step(evidence=("e1",), tokens=0), {"e1": 1.0})

        assert retrieval.retrieval_gain < extraction.information_gain

    def test_tokens_and_calls_both_cost(self) -> None:
        call = ToolCall(tool="t", query="q", documents_returned=0, succeeded=True)
        cheap = StepReward.earned(Scenario.step(tokens=0), {})
        pricey = StepReward.earned(Scenario.step(tokens=5000, calls=(call,)), {})

        assert pricey.cost > cheap.cost

    def test_a_failed_lookup_costs_more_than_a_successful_one(self) -> None:
        good = ToolCall(tool="t", query="q", documents_returned=0, succeeded=True)
        bad = ToolCall(tool="t", query="q", documents_returned=0, succeeded=False)

        succeeded = StepReward.earned(Scenario.step(tokens=0, calls=(good,)), {})
        failed = StepReward.earned(Scenario.step(tokens=0, calls=(bad,)), {})

        assert failed.cost > succeeded.cost

    def test_the_series_covers_every_recorded_step(self) -> None:
        investigation = Scenario.investigation()
        for index in range(3):
            investigation.record_step(Scenario.step(index=index))

        assert len(StepReward.series(investigation)) == 3

    def test_carries_a_version_stamp(self) -> None:
        assert StepReward.earned(Scenario.step(), {}).version == StepReward.VERSION


class TestEpisodeRewardCorrectness:
    def test_the_expected_conclusion_earns_full_credit(self) -> None:
        reward = EpisodeReward.earned(Scenario.investigation(), Scenario.case())

        assert reward.correctness == 1.0

    def test_a_supported_versus_partially_supported_miss_earns_half(self) -> None:
        reward = EpisodeReward.earned(
            Scenario.investigation(judgment=Judgment.PARTIALLY_SUPPORTED), Scenario.case()
        )

        assert reward.correctness == EpisodeReward.PARTIAL_CREDIT

    def test_abstaining_where_a_verdict_was_available_earns_nothing(self) -> None:
        reward = EpisodeReward.earned(
            Scenario.investigation(judgment=Judgment.INSUFFICIENT_EVIDENCE), Scenario.case()
        )

        assert reward.correctness == 0.0

    def test_committing_where_abstention_was_the_only_honest_answer_earns_nothing(self) -> None:
        reward = EpisodeReward.earned(
            Scenario.investigation(judgment=Judgment.SUPPORTED),
            Scenario.case(expected=Judgment.INSUFFICIENT_EVIDENCE),
        )

        assert reward.correctness == 0.0


class TestEpisodeRewardCalibration:
    def test_confident_and_right_inside_the_band_scores_well(self) -> None:
        reward = EpisodeReward.earned(
            Scenario.investigation(probability=0.85), Scenario.case(lowest=0.6, highest=0.9)
        )

        assert reward.calibration > 0.9

    def test_confident_and_wrong_is_punished(self) -> None:
        reward = EpisodeReward.earned(
            Scenario.investigation(judgment=Judgment.REFUTED, probability=0.97),
            Scenario.case(expected=Judgment.SUPPORTED),
        )

        assert reward.calibration < 0.1

    def test_hedging_outside_the_defensible_band_loses_half_the_credit(self) -> None:
        inside = EpisodeReward.earned(
            Scenario.investigation(probability=0.8), Scenario.case(lowest=0.7, highest=0.9)
        )
        hedged = EpisodeReward.earned(
            Scenario.investigation(probability=0.5), Scenario.case(lowest=0.7, highest=0.9)
        )

        assert inside.calibration > hedged.calibration


class TestEpisodeRewardGrounding:
    def test_citation_integrity_is_a_gate_not_a_gradient(self) -> None:
        clean = EpisodeReward.earned(Scenario.investigation(), Scenario.case())

        assert clean.citation_integrity == 1.0

    def test_a_citation_with_nothing_behind_it_zeroes_the_gate(self) -> None:
        investigation = Scenario.investigation()
        forged = Evidence(
            assertion="invented",
            document_url="https://nowhere.example/x",
            source_domain="nowhere.example",
            credibility=InformationCredibility.CONFIRMED,
        )
        investigation.evidence[forged.identifier] = forged

        reward = EpisodeReward.earned(investigation, Scenario.case())

        assert reward.citation_integrity == 0.0

    def test_a_single_source_is_discounted_against_several(self) -> None:
        narrow = Scenario.investigation()
        Scenario.with_evidence(narrow)
        broad = Scenario.investigation()
        Scenario.with_evidence(broad)
        Scenario.with_evidence(broad, url=Scenario.OTHER, assertion="Confirmed elsewhere.")

        narrow_reward = EpisodeReward.earned(narrow, Scenario.case())
        broad_reward = EpisodeReward.earned(broad, Scenario.case())

        assert broad_reward.evidence_quality > narrow_reward.evidence_quality

    def test_no_evidence_scores_no_evidence_quality(self) -> None:
        reward = EpisodeReward.earned(Scenario.investigation(), Scenario.case())

        assert reward.evidence_quality == 0.0


class TestEpisodeRewardTotal:
    def test_the_efficiency_penalty_is_capped(self) -> None:
        investigation = Scenario.investigation()
        investigation.record_step(Scenario.step(tokens=10_000_000))

        reward = EpisodeReward.earned(investigation, Scenario.case())

        assert reward.efficiency_penalty == EpisodeReward.MAXIMUM_EFFICIENCY_PENALTY

    def test_the_total_never_goes_negative(self) -> None:
        investigation = Scenario.investigation(judgment=Judgment.REFUTED, probability=0.99)
        investigation.record_step(Scenario.step(tokens=10_000_000))

        reward = EpisodeReward.earned(investigation, Scenario.case())

        assert reward.total() >= 0.0

    def test_a_well_run_investigation_beats_a_lucky_guess(self) -> None:
        thorough = Scenario.investigation(probability=0.8)
        Scenario.with_evidence(thorough)
        Scenario.with_evidence(thorough, url=Scenario.OTHER, assertion="Corroborated.")
        guess = Scenario.investigation(probability=0.8)

        assert EpisodeReward.earned(thorough, Scenario.case()).total() > (
            EpisodeReward.earned(guess, Scenario.case()).total()
        )

    def test_carries_a_version_stamp(self) -> None:
        reward = EpisodeReward.earned(Scenario.investigation(), Scenario.case())

        assert reward.version == EpisodeReward.VERSION


class TestDiagnosis:
    def test_a_fabricated_citation_outranks_every_other_tag(self) -> None:
        investigation = Scenario.investigation(judgment=Judgment.REFUTED)
        forged = Evidence(
            assertion="invented",
            document_url="https://nowhere.example/x",
            source_domain="nowhere.example",
            credibility=InformationCredibility.CONFIRMED,
        )
        investigation.evidence[forged.identifier] = forged

        assert Diagnosis.of(investigation, Scenario.case()) is FailureMode.FABRICATED_CITATION

    def test_mostly_failed_lookups_are_tagged_as_a_tool_failure(self) -> None:
        investigation = Scenario.investigation()
        investigation.record_step(
            Scenario.step(
                calls=(
                    ToolCall(tool="t", query="q", documents_returned=0, succeeded=False),
                    ToolCall(tool="t", query="q", documents_returned=0, succeeded=False),
                    ToolCall(tool="t", query="q", documents_returned=1, succeeded=True),
                )
            )
        )

        assert Diagnosis.of(investigation, Scenario.case()) is FailureMode.TOOL_FAILURE

    def test_a_prior_about_someone_else_beside_a_wrong_answer_is_memory_interference(self) -> None:
        investigation = Scenario.investigation(judgment=Judgment.REFUTED)
        Scenario.with_evidence(investigation)
        Scenario.with_evidence(investigation, url=Scenario.OTHER, assertion="Also.")
        investigation.recalled_priors = (
            Recollection(
                text="An earlier investigation of Beta Ltd concluded refuted.",
                from_run="run-0",
                about="Beta Ltd",
                similarity=0.7,
            ),
        )

        assert Diagnosis.of(investigation, Scenario.case()) is FailureMode.MEMORY_INTERFERENCE

    def test_a_prior_about_the_same_subject_is_not_interference(self) -> None:
        investigation = Scenario.investigation()
        investigation.recalled_priors = (
            Recollection(
                text="An earlier investigation of Acme Corp concluded supported.",
                from_run="run-0",
                about="Acme Corp",
                similarity=1.0,
            ),
        )

        assert Diagnosis.misleading_recalls(investigation) == ()

    def test_a_wrong_answer_on_thin_evidence_is_weak_evidence(self) -> None:
        investigation = Scenario.investigation(judgment=Judgment.REFUTED)

        assert Diagnosis.of(investigation, Scenario.case()) is FailureMode.WEAK_EVIDENCE

    def test_a_halted_investigation_never_converged(self) -> None:
        investigation = Scenario.investigation()
        Scenario.with_evidence(investigation)
        Scenario.with_evidence(investigation, url=Scenario.OTHER, assertion="Also.")
        investigation.phase = InvestigationPhase.HALTED

        assert Diagnosis.of(investigation, Scenario.case()) is FailureMode.NO_CONVERGENCE

    def test_overclaiming_is_caught_even_when_the_answer_is_right(self) -> None:
        investigation = Scenario.investigation(probability=0.99)
        Scenario.with_evidence(investigation)
        Scenario.with_evidence(investigation, url=Scenario.OTHER, assertion="Also.")

        assert Diagnosis.of(investigation, Scenario.case(highest=0.9)) is FailureMode.OVERCONFIDENT

    def test_a_clean_investigation_carries_no_failure_tag(self) -> None:
        investigation = Scenario.investigation(probability=0.8)
        Scenario.with_evidence(investigation)
        Scenario.with_evidence(investigation, url=Scenario.OTHER, assertion="Also.")

        assert Diagnosis.of(investigation, Scenario.case()) is FailureMode.NONE

    def test_the_priority_order_is_fixed_and_inspectable(self) -> None:
        order = Diagnosis.in_priority_order(Scenario.investigation(), Scenario.case())

        assert [mode for _, mode in order][:2] == [
            FailureMode.FABRICATED_CITATION,
            FailureMode.TOOL_FAILURE,
        ]


class TestCaseOutcome:
    def _outcome(self) -> CaseOutcome:
        investigation = Scenario.investigation()
        Scenario.with_evidence(investigation)
        investigation.record_step(Scenario.step(evidence=("e1",)))
        return CaseOutcome.scored(investigation, Scenario.case())

    def test_records_everything_the_report_needs(self) -> None:
        outcome = self._outcome()

        assert outcome.case_id == "c1"
        assert outcome.correct
        assert outcome.evidence_count == 1
        assert outcome.step_rewards

    def test_reward_progression_is_cumulative(self) -> None:
        investigation = Scenario.investigation()
        identifier = Scenario.with_evidence(investigation)
        for index in range(3):
            investigation.record_step(
                Scenario.step(index=index, evidence=(identifier,) if index == 1 else ())
            )

        progression = CaseOutcome.scored(investigation, Scenario.case()).reward_progression()

        assert len(progression) == 3
        assert progression[1] > progression[0]

    def test_reward_per_thousand_tokens_is_zero_rather_than_infinite_at_zero_spend(self) -> None:
        investigation = Scenario.investigation()

        outcome = CaseOutcome.scored(investigation, Scenario.case())

        assert outcome.reward_per_thousand_tokens() == 0.0


class TestBenchmarkRun:
    def _run(self, outcomes: tuple[CaseOutcome, ...]) -> BenchmarkRun:
        return BenchmarkRun(memory_mode=MemoryMode.LONG, outcomes=outcomes)

    def _outcome(
        self,
        correct: bool = True,
        misleading: int = 0,
        expected: Judgment = Judgment.SUPPORTED,
        reached: Judgment = Judgment.SUPPORTED,
    ) -> CaseOutcome:
        investigation = Scenario.investigation(judgment=reached)
        Scenario.with_evidence(investigation)
        investigation.recalled_priors = tuple(
            Recollection(text="t", from_run=f"r{i}", about="Someone Else", similarity=0.7)
            for i in range(misleading)
        )
        outcome = CaseOutcome.scored(investigation, Scenario.case(expected=expected))
        assert outcome.correct == correct
        return outcome

    def test_accuracy_counts_every_case_including_failures(self) -> None:
        run = self._run(
            (
                self._outcome(correct=True),
                self._outcome(
                    correct=False, expected=Judgment.REFUTED, reached=Judgment.SUPPORTED
                ),
            )
        )

        assert run.accuracy() == 0.5

    def test_an_empty_run_reports_zero_rather_than_dividing_by_nothing(self) -> None:
        run = self._run(())

        assert run.accuracy() == 0.0
        assert run.mean_reward() == 0.0
        assert run.harmful_retrieval_rate() == 0.0

    def test_irrelevant_retrieval_is_counted_separately_from_harmful(self) -> None:
        run = self._run(
            (
                self._outcome(correct=True, misleading=1),
                self._outcome(
                    correct=False,
                    misleading=1,
                    expected=Judgment.REFUTED,
                    reached=Judgment.SUPPORTED,
                ),
            )
        )

        assert run.irrelevant_retrieval_rate() == 1.0
        assert run.harmful_retrieval_rate() == 0.5

    def test_failure_counts_omit_modes_that_never_occurred(self) -> None:
        run = self._run((self._outcome(correct=True),))

        assert FailureMode.TOOL_FAILURE not in run.failure_counts()

    def test_abstention_accuracy_ignores_cases_where_abstention_never_arose(self) -> None:
        run = self._run((self._outcome(correct=True),))

        assert run.abstention_accuracy() == 1.0

    def test_abstention_accuracy_catches_a_missed_abstention(self) -> None:
        run = self._run(
            (
                self._outcome(
                    correct=False,
                    expected=Judgment.INSUFFICIENT_EVIDENCE,
                    reached=Judgment.SUPPORTED,
                ),
            )
        )

        assert run.abstention_accuracy() == 0.0


class TestBudgetedRun:
    def test_a_halted_episode_still_counts_in_the_denominator(self) -> None:
        investigation = Investigation.open(
            run_id="r1",
            subject=Company(name="Acme Corp"),
            memory_mode=MemoryMode.NONE,
            budget=Budget(max_steps=1),
        )
        investigation.leads.append(Lead(question="unanswered"))
        investigation.phase = InvestigationPhase.HALTED

        outcome = CaseOutcome.scored(investigation, Scenario.case())
        run = BenchmarkRun(memory_mode=MemoryMode.NONE, outcomes=(outcome,))

        assert outcome.halted
        assert run.accuracy() == 0.0
        assert len(run.outcomes) == 1
