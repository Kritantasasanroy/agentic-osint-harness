import hashlib
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from osint_harness.domain.provenance import InformationCredibility, SourceReliability


class Consistency(StrEnum):
    """How one piece of evidence bears on one hypothesis, in the ACH sense."""

    CONSISTENT = "consistent"
    INCONSISTENT = "inconsistent"
    NOT_APPLICABLE = "not_applicable"


class Judgment(StrEnum):
    """The analytic conclusion an investigation reached about its subject."""

    SUPPORTED = "supported"
    REFUTED = "refuted"
    PARTIALLY_SUPPORTED = "partially_supported"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class VerdictStandard(BaseModel):
    """What reaching each possible judgment asserts about one kind of subject."""

    model_config = ConfigDict(frozen=True)

    supported: str = Field(min_length=1)
    refuted: str = Field(min_length=1)
    partially_supported: str = Field(min_length=1)
    insufficient_evidence: str = Field(min_length=1)

    def meaning(self, judgment: Judgment) -> str:
        """What reaching this judgment asserts."""
        match judgment:
            case Judgment.SUPPORTED:
                return self.supported
            case Judgment.REFUTED:
                return self.refuted
            case Judgment.PARTIALLY_SUPPORTED:
                return self.partially_supported
            case Judgment.INSUFFICIENT_EVIDENCE:
                return self.insufficient_evidence


class ConfidenceBand(StrEnum):
    """An ICD 203 word of estimative probability, carrying its own numeric range."""

    ALMOST_NO_CHANCE = "almost no chance"
    VERY_UNLIKELY = "very unlikely"
    UNLIKELY = "unlikely"
    ROUGHLY_EVEN_CHANCE = "roughly even chance"
    LIKELY = "likely"
    VERY_LIKELY = "very likely"
    ALMOST_CERTAIN = "almost certain"

    @classmethod
    def boundaries(cls) -> tuple[float, ...]:
        """The cut points partitioning the ICD 203 scale. One more than there are bands."""
        return (0.01, 0.05, 0.20, 0.45, 0.55, 0.80, 0.95, 0.99)

    def ordinal(self) -> int:
        """This band's position on the scale, lowest probability first."""
        return tuple(ConfidenceBand).index(self)

    def probability_range(self) -> tuple[float, float]:
        """The probability interval this band denotes under ICD 203."""
        cuts = self.boundaries()
        position = self.ordinal()
        return (cuts[position], cuts[position + 1])

    @classmethod
    def for_probability(cls, probability: float) -> "ConfidenceBand":
        """The band whose range contains this probability, saturating at either extreme."""
        for band in cls:
            if probability <= band.probability_range()[1]:
                return band
        return cls.ALMOST_CERTAIN


class Evidence(BaseModel):
    """A single assertion extracted from a document, bearing on the investigation's question."""

    model_config = ConfigDict(frozen=True)

    assertion: str = Field(min_length=1)
    document_url: str = Field(min_length=1)
    source_domain: str = Field(min_length=1)
    credibility: InformationCredibility
    rationale: str = ""

    @property
    def identifier(self) -> str:
        """A stable content-derived identifier, so the same assertion is never counted twice."""
        digest = hashlib.sha256(f"{self.document_url}\n{self.assertion}".encode())
        return digest.hexdigest()[:12]

    def weight(self, reliability: SourceReliability) -> float:
        """How much this assertion counts, combining publisher track record and item credibility."""
        return reliability.weight() * self.credibility.weight()


class Hypothesis(BaseModel):
    """A candidate answer to the central question, stated so that it could be disproved."""

    statement: str = Field(min_length=1)
    consistency: dict[str, Consistency] = Field(default_factory=dict)
    origin: str = "direction"

    def judge(self, evidence_id: str, consistency: Consistency) -> None:
        """Record how one piece of evidence bears on this hypothesis."""
        self.consistency[evidence_id] = consistency

    def inconsistency_score(self, weights: Mapping[str, float]) -> float:
        """Weighted disconfirming evidence. ACH ranks by this: least disconfirmed wins."""
        return sum(
            weights.get(evidence_id, 0.0)
            for evidence_id, verdict in self.consistency.items()
            if verdict is Consistency.INCONSISTENT
        )

    def support_score(self, weights: Mapping[str, float]) -> float:
        """Weighted confirming evidence. Reported for transparency, never used to rank."""
        return sum(
            weights.get(evidence_id, 0.0)
            for evidence_id, verdict in self.consistency.items()
            if verdict is Consistency.CONSISTENT
        )

    def diagnostic_evidence(self) -> tuple[str, ...]:
        """Evidence this hypothesis takes a position on, which is what makes it falsifiable."""
        return tuple(
            evidence_id
            for evidence_id, verdict in self.consistency.items()
            if verdict is not Consistency.NOT_APPLICABLE
        )


class Findings(BaseModel):
    """The narrative half of the analytic product: what was concluded, what is missing."""

    model_config = ConfigDict(frozen=True)

    summary: str = ""
    key_findings: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    def is_written(self) -> bool:
        """Whether dissemination has actually produced a report."""
        return bool(self.summary)


class Assessment(BaseModel):
    """The conclusion at one moment: a judgment, a confidence, and the reasoning for both."""

    model_config = ConfigDict(frozen=True)

    judgment: Judgment
    leading_hypothesis: str
    probability: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    made_in_phase: str = ""

    def band(self) -> ConfidenceBand:
        """The ICD 203 band this assessment's probability falls into."""
        return ConfidenceBand.for_probability(self.probability)

    def brier_score(self, was_correct: bool) -> float:
        """Squared error of the stated probability against the outcome. Lower is better."""
        return (self.probability - (1.0 if was_correct else 0.0)) ** 2

    def agrees_with(self, other: "Assessment") -> bool:
        """Whether two assessments reached the same judgment, ignoring confidence."""
        return self.judgment is other.judgment
