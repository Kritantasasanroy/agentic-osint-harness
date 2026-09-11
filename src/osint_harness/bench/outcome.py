from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from osint_harness.bench.case import BenchmarkCase, CaseTrap
from osint_harness.bench.reward import EpisodeReward, StepReward
from osint_harness.domain.analysis import Judgment
from osint_harness.domain.investigation import (
    Investigation,
    InvestigationPhase,
    MemoryMode,
    Recollection,
)
from osint_harness.storage import Persisted


class FailureMode(StrEnum):
    """Where an investigation broke down. Exactly one is assigned, by fixed priority."""

    NONE = "none"
    FABRICATED_CITATION = "fabricated_citation"
    TOOL_FAILURE = "tool_failure"
    MEMORY_INTERFERENCE = "memory_interference"
    WEAK_EVIDENCE = "weak_evidence"
    SOURCE_SELECTION = "source_selection"
    PREMATURE_STOP = "premature_stop"
    NO_CONVERGENCE = "no_convergence"
    OVERCONFIDENT = "overconfident"
    UNDERCONFIDENT = "underconfident"

    def is_failure(self) -> bool:
        """Whether this tag records something going wrong."""
        return self is not FailureMode.NONE


class Diagnosis(BaseModel):
    """Reads a finished investigation and names the single way it went wrong.

    The order below is a priority, not a list. A case that could carry two tags always gets the
    same one, so the taxonomy stays deterministic and counts across runs can be compared. Ordering
    runs from the most damaging and least ambiguous (a citation with nothing behind it) down to
    matters of degree (confidence stated slightly outside the defensible band).
    """

    FAILED_CALL_SHARE: ClassVar[float] = 0.5
    UNSTABLE_VERDICT_CHANGES: ClassVar[int] = 3
    HASTY_STEP_COUNT: ClassVar[int] = 6

    @classmethod
    def of(cls, investigation: Investigation, case: BenchmarkCase) -> FailureMode:
        """The one failure mode this episode is tagged with: the first that applies."""
        for detected, mode in cls.in_priority_order(investigation, case):
            if detected:
                return mode
        return FailureMode.NONE

    @classmethod
    def in_priority_order(
        cls, investigation: Investigation, case: BenchmarkCase
    ) -> tuple[tuple[bool, FailureMode], ...]:
        """Every check that could apply, most damaging first.

        The priority is a data structure rather than a chain of branches, so the order it resolves
        ties in is visible and reviewable instead of buried in control flow.
        """
        assessment = investigation.latest_assessment()
        wrong = not case.was_reached(assessment.judgment)
        thin = not investigation.has_sufficient_evidence()
        return (
            (bool(investigation.ungrounded_citations()), FailureMode.FABRICATED_CITATION),
            (cls._mostly_failed_lookups(investigation), FailureMode.TOOL_FAILURE),
            (
                wrong and bool(cls.misleading_recalls(investigation)),
                FailureMode.MEMORY_INTERFERENCE,
            ),
            (wrong and thin, FailureMode.WEAK_EVIDENCE),
            (wrong and investigation.source_diversity() <= 1, FailureMode.SOURCE_SELECTION),
            (wrong and cls._stopped_early(investigation), FailureMode.PREMATURE_STOP),
            (cls._never_settled(investigation), FailureMode.NO_CONVERGENCE),
            (case.confidence.overclaimed(assessment.probability), FailureMode.OVERCONFIDENT),
            (case.confidence.underclaimed(assessment.probability), FailureMode.UNDERCONFIDENT),
        )

    @classmethod
    def misleading_recalls(cls, investigation: Investigation) -> tuple[Recollection, ...]:
        """Priors recalled about a different subject than the one under investigation.

        This is what harmful retrieval actually looks like: the archive matched on a name and
        handed back an episode about somebody else. It is recorded whether or not it changed the
        outcome, so irrelevant retrieval and harmful retrieval can be counted separately.
        """
        descriptor = investigation.subject.descriptor()
        return tuple(
            prior for prior in investigation.recalled_priors if prior.about != descriptor
        )

    @classmethod
    def _mostly_failed_lookups(cls, investigation: Investigation) -> bool:
        calls = investigation.tool_calls()
        if not calls:
            return False
        failed = sum(1 for call in calls if not call.succeeded)
        return failed / len(calls) > cls.FAILED_CALL_SHARE

    @classmethod
    def _stopped_early(cls, investigation: Investigation) -> bool:
        return (
            len(investigation.steps) <= cls.HASTY_STEP_COUNT
            and bool(investigation.open_leads())
        )

    @classmethod
    def _never_settled(cls, investigation: Investigation) -> bool:
        return (
            investigation.phase is InvestigationPhase.HALTED
            or investigation.verdict_changes() >= cls.UNSTABLE_VERDICT_CHANGES
        )


class CaseOutcome(BaseModel):
    """Everything one episode produced, reduced to what the report needs.

    Every field here is recomputable from the stored trajectory. Nothing is reported that could not
    be regenerated by re-running this scoring over the saved investigation.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    run_id: str
    memory_mode: MemoryMode
    trap: CaseTrap
    expected: Judgment
    reached: Judgment
    correct: bool
    probability: float = Field(ge=0.0, le=1.0)
    within_defensible_band: bool
    brier: float = Field(ge=0.0, le=1.0)
    reward: EpisodeReward
    step_rewards: tuple[StepReward, ...] = ()
    failure: FailureMode = FailureMode.NONE
    verdict_changes: int = Field(ge=0)
    assessments_to_stable_verdict: int = Field(ge=0)
    steps_taken: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tokens: int = Field(ge=0)
    seconds: float = Field(ge=0.0)
    source_diversity: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    misleading_recalls: int = Field(default=0, ge=0)
    halted: bool = False

    @classmethod
    def scored(cls, investigation: Investigation, case: BenchmarkCase) -> "CaseOutcome":
        """Score a finished investigation against its case."""
        assessment = investigation.latest_assessment()
        correct = case.was_reached(assessment.judgment)
        return cls(
            case_id=case.case_id,
            run_id=investigation.run_id,
            memory_mode=investigation.memory_mode,
            trap=case.trap,
            expected=case.expected,
            reached=assessment.judgment,
            correct=correct,
            probability=assessment.probability,
            within_defensible_band=case.confidence.contains(assessment.probability),
            brier=assessment.brier_score(was_correct=correct),
            reward=EpisodeReward.earned(investigation, case),
            step_rewards=StepReward.series(investigation),
            failure=Diagnosis.of(investigation, case),
            verdict_changes=investigation.verdict_changes(),
            assessments_to_stable_verdict=investigation.assessments_to_stable_verdict(),
            steps_taken=len(investigation.steps),
            tool_calls=len(investigation.tool_calls()),
            tokens=investigation.token_cost(),
            seconds=investigation.elapsed_seconds(),
            source_diversity=investigation.source_diversity(),
            evidence_count=len(investigation.evidence),
            misleading_recalls=len(Diagnosis.misleading_recalls(investigation)),
            halted=investigation.phase is InvestigationPhase.HALTED,
        )

    def reward_progression(self) -> tuple[float, ...]:
        """Cumulative reward across the trajectory, which is what progression is read from."""
        running = 0.0
        progression: list[float] = []
        for step in self.step_rewards:
            running += step.total()
            progression.append(running)
        return tuple(progression)

    def reward_per_thousand_tokens(self) -> float:
        """Quality bought per unit of spend. Zero-token episodes score zero, not infinity."""
        if self.tokens == 0:
            return 0.0
        return self.reward.total() / (self.tokens / 1000.0)


class BenchmarkRun(Persisted):
    """One sweep of the benchmark in one memory mode.

    A crashed or halted episode stays in the denominator with a failure tag. Dropping it would be
    the simplest way to make a mediocre agent look excellent, so it is not available here.
    """

    memory_mode: MemoryMode
    outcomes: tuple[CaseOutcome, ...] = ()

    def accuracy(self) -> float:
        """Share of cases where the expected conclusion was reached."""
        if not self.outcomes:
            return 0.0
        return sum(1 for outcome in self.outcomes if outcome.correct) / len(self.outcomes)

    def mean_reward(self) -> float:
        """Average episode reward across every case, failures included."""
        if not self.outcomes:
            return 0.0
        return sum(outcome.reward.total() for outcome in self.outcomes) / len(self.outcomes)

    def mean_brier(self) -> float:
        """Average calibration error. Lower is better."""
        if not self.outcomes:
            return 0.0
        return sum(outcome.brier for outcome in self.outcomes) / len(self.outcomes)

    def defensible_confidence_rate(self) -> float:
        """Share of cases whose stated confidence a competent analyst could defend."""
        if not self.outcomes:
            return 0.0
        within = sum(1 for outcome in self.outcomes if outcome.within_defensible_band)
        return within / len(self.outcomes)

    def abstention_accuracy(self) -> float:
        """How reliably the agent declined exactly where declining was the right answer."""
        judged = [
            outcome
            for outcome in self.outcomes
            if outcome.expected is Judgment.INSUFFICIENT_EVIDENCE
            or outcome.reached is Judgment.INSUFFICIENT_EVIDENCE
        ]
        if not judged:
            return 1.0
        agreed = sum(
            1
            for outcome in judged
            if (outcome.expected is Judgment.INSUFFICIENT_EVIDENCE)
            == (outcome.reached is Judgment.INSUFFICIENT_EVIDENCE)
        )
        return agreed / len(judged)

    def irrelevant_retrieval_rate(self) -> float:
        """Share of episodes handed a prior about some other subject, harmful or not."""
        if not self.outcomes:
            return 0.0
        affected = sum(1 for outcome in self.outcomes if outcome.misleading_recalls > 0)
        return affected / len(self.outcomes)

    def harmful_retrieval_rate(self) -> float:
        """Share of episodes where a prior about another subject accompanied a wrong conclusion."""
        if not self.outcomes:
            return 0.0
        harmed = sum(
            1
            for outcome in self.outcomes
            if outcome.misleading_recalls > 0 and not outcome.correct
        )
        return harmed / len(self.outcomes)

    def failure_counts(self) -> dict[FailureMode, int]:
        """How many episodes carried each failure tag."""
        counts: dict[FailureMode, int] = dict.fromkeys(FailureMode, 0)
        for outcome in self.outcomes:
            counts[outcome.failure] += 1
        return {mode: count for mode, count in counts.items() if count}

    def mean_assessments_to_stable_verdict(self) -> float:
        """How quickly conclusions settled, on average."""
        if not self.outcomes:
            return 0.0
        return sum(o.assessments_to_stable_verdict for o in self.outcomes) / len(self.outcomes)

    def mean_verdict_changes(self) -> float:
        """How much the conclusion moved before settling."""
        if not self.outcomes:
            return 0.0
        return sum(o.verdict_changes for o in self.outcomes) / len(self.outcomes)

    def total_tokens(self) -> int:
        """Total spend across the sweep."""
        return sum(outcome.tokens for outcome in self.outcomes)

    def reward_per_thousand_tokens(self) -> float:
        """Quality bought per unit of spend across the whole sweep."""
        if self.total_tokens() == 0:
            return 0.0
        return sum(o.reward.total() for o in self.outcomes) / (self.total_tokens() / 1000.0)
