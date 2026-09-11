from collections.abc import Mapping
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from osint_harness.bench.case import BenchmarkCase
from osint_harness.domain.analysis import Judgment
from osint_harness.domain.investigation import Investigation, InvestigationPhase, Step


class StepReward(BaseModel):
    """What one step earned: what it learned, less what it cost.

    Decomposed rather than scalar because the brief asks where investigations break down, and a
    single number cannot answer that. Computed offline from the recorded step, never inside the
    loop: an agent that can observe its own reward is being measured on score-chasing rather than
    on judgment.
    """

    model_config = ConfigDict(frozen=True)

    VERSION: ClassVar[str] = "step-reward-v1"
    EVIDENCE_CREDIT: ClassVar[float] = 1.0
    RETRIEVAL_CREDIT: ClassVar[float] = 0.05
    TOKEN_COST_PER_THOUSAND: ClassVar[float] = 0.02
    CALL_COST: ClassVar[float] = 0.02
    FAILED_CALL_COST: ClassVar[float] = 0.05

    step_index: int = Field(ge=0)
    phase: InvestigationPhase
    information_gain: float
    retrieval_gain: float
    cost: float
    version: str = VERSION

    def total(self) -> float:
        """The step's net reward."""
        return self.information_gain + self.retrieval_gain - self.cost

    @classmethod
    def earned(
        cls,
        step: Step,
        weights: Mapping[str, float],
        already_credited: frozenset[str] = frozenset(),
    ) -> "StepReward":
        """Score one recorded step against the Admiralty weight of the evidence it produced.

        Each distinct piece of evidence is credited exactly once across the whole trajectory.
        Evidence identifiers are content-derived, so an extraction that repeats an assertion
        already recorded returns the same identifier; counting occurrences rather than distinct
        items would let a repetition inflate the reward above the evidence the episode actually
        holds. Retrieval is credited far more lightly than extraction, because fetching documents
        nobody reads is activity rather than progress.
        """
        retrieved = sum(call.documents_returned for call in step.tool_calls if call.succeeded)
        failed = len(step.failed_tool_calls())
        fresh = cls.newly_credited(step, already_credited)
        return cls(
            step_index=step.index,
            phase=step.phase,
            information_gain=cls.EVIDENCE_CREDIT
            * sum(weights.get(identifier, 0.0) for identifier in fresh),
            retrieval_gain=cls.RETRIEVAL_CREDIT * retrieved,
            cost=(
                cls.TOKEN_COST_PER_THOUSAND * (step.tokens() / 1000.0)
                + cls.CALL_COST * len(step.tool_calls)
                + cls.FAILED_CALL_COST * failed
            ),
        )

    @classmethod
    def newly_credited(cls, step: Step, already_credited: frozenset[str]) -> tuple[str, ...]:
        """The evidence this step introduced that no earlier step has been paid for."""
        return tuple(
            identifier
            for identifier in dict.fromkeys(step.evidence_added)
            if identifier not in already_credited
        )

    @classmethod
    def series(cls, investigation: Investigation) -> tuple["StepReward", ...]:
        """The whole trajectory scored step by step, which is what progression is read from."""
        weights = investigation.evidence_weights()
        credited: set[str] = set()
        rewards: list[StepReward] = []
        for step in investigation.steps:
            frozen = frozenset(credited)
            rewards.append(cls.earned(step, weights, frozen))
            credited.update(cls.newly_credited(step, frozen))
        return tuple(rewards)


class EpisodeReward(BaseModel):
    """What a whole investigation earned, decomposed so a poor score says which part failed.

    The weights encode a position: being right matters most, but a correct verdict stated at
    indefensible confidence, or resting on a single source, or carrying a citation with nothing
    behind it, is not worth the same as one that is properly grounded. Citation integrity is scored
    as a gate rather than a gradient, because a fabricated citation is not a small error.
    """

    model_config = ConfigDict(frozen=True)

    VERSION: ClassVar[str] = "episode-reward-v1"
    CORRECTNESS_WEIGHT: ClassVar[float] = 0.40
    CALIBRATION_WEIGHT: ClassVar[float] = 0.25
    EVIDENCE_WEIGHT: ClassVar[float] = 0.20
    CITATION_WEIGHT: ClassVar[float] = 0.15
    PARTIAL_CREDIT: ClassVar[float] = 0.5
    DIVERSITY_TARGET: ClassVar[int] = 3
    TOKENS_PER_PENALTY_POINT: ClassVar[float] = 200_000.0
    CALLS_PER_PENALTY_POINT: ClassVar[float] = 40.0
    MAXIMUM_EFFICIENCY_PENALTY: ClassVar[float] = 0.20

    correctness: float = Field(ge=0.0, le=1.0)
    calibration: float = Field(ge=0.0, le=1.0)
    evidence_quality: float = Field(ge=0.0, le=1.0)
    citation_integrity: float = Field(ge=0.0, le=1.0)
    efficiency_penalty: float = Field(ge=0.0)
    version: str = VERSION

    def total(self) -> float:
        """The episode's net reward, never below zero."""
        earned = (
            self.CORRECTNESS_WEIGHT * self.correctness
            + self.CALIBRATION_WEIGHT * self.calibration
            + self.EVIDENCE_WEIGHT * self.evidence_quality
            + self.CITATION_WEIGHT * self.citation_integrity
        )
        return max(0.0, earned - self.efficiency_penalty)

    @classmethod
    def earned(cls, investigation: Investigation, case: BenchmarkCase) -> "EpisodeReward":
        """Score a finished investigation against what a competent analyst should have concluded."""
        assessment = investigation.latest_assessment()
        correctness = cls._correctness(assessment.judgment, case.expected)
        return cls(
            correctness=correctness,
            calibration=cls._calibration(investigation, case, correctness),
            evidence_quality=cls._evidence_quality(investigation),
            citation_integrity=0.0 if investigation.ungrounded_citations() else 1.0,
            efficiency_penalty=cls._efficiency_penalty(investigation),
        )

    @classmethod
    def _correctness(cls, reached: Judgment, expected: Judgment) -> float:
        """Full credit for the right answer, half for a defensible near miss, nothing otherwise.

        Partial credit is confined to the supported/partially-supported boundary, which is a matter
        of degree. Abstaining where a verdict was available, or committing where abstention was the
        only honest answer, earns nothing: those are the two failures this harness exists to catch.
        """
        if reached is expected:
            return 1.0
        near_misses = {
            (Judgment.SUPPORTED, Judgment.PARTIALLY_SUPPORTED),
            (Judgment.PARTIALLY_SUPPORTED, Judgment.SUPPORTED),
            (Judgment.REFUTED, Judgment.PARTIALLY_SUPPORTED),
            (Judgment.PARTIALLY_SUPPORTED, Judgment.REFUTED),
        }
        return cls.PARTIAL_CREDIT if (reached, expected) in near_misses else 0.0

    @classmethod
    def _calibration(
        cls, investigation: Investigation, case: BenchmarkCase, correctness: float
    ) -> float:
        """Half from the Brier score, half from whether the confidence was defensible at all.

        Brier alone rewards hedging everything to the middle. Requiring the stated confidence to
        fall inside the band a competent analyst could defend on this case penalises that, and
        penalises overclaiming on a case the evidence cannot carry.
        """
        assessment = investigation.latest_assessment()
        brier = assessment.brier_score(was_correct=correctness >= 1.0)
        defensible = 1.0 if case.confidence.contains(assessment.probability) else 0.0
        return 0.5 * (1.0 - brier) + 0.5 * defensible

    @classmethod
    def _evidence_quality(cls, investigation: Investigation) -> float:
        """How strong the evidence was, discounted when it all came from one place."""
        weights = investigation.evidence_weights()
        if not weights:
            return 0.0
        strength = sum(weights.values()) / len(weights)
        diversity = min(1.0, investigation.source_diversity() / cls.DIVERSITY_TARGET)
        return strength * diversity

    @classmethod
    def _efficiency_penalty(cls, investigation: Investigation) -> float:
        """A capped charge for what the answer cost, so cheap wrong answers cannot win."""
        spend = (
            investigation.token_cost() / cls.TOKENS_PER_PENALTY_POINT
            + len(investigation.tool_calls()) / cls.CALLS_PER_PENALTY_POINT
        )
        return min(cls.MAXIMUM_EFFICIENCY_PENALTY, spend)
