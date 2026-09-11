from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from osint_harness.domain.analysis import Judgment
from osint_harness.domain.subject import AnySubject, SubjectKind


class CaseTrap(StrEnum):
    """What a benchmark case is designed to catch an investigator doing wrong."""

    NONE = "none"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    SAME_NAME = "same_name"
    DISAMBIGUATION = "disambiguation"
    FALSE_PREMISE = "false_premise"
    ADVERSE_MEDIA = "adverse_media"
    POPULAR_MYTH = "popular_myth"


class DefensibleConfidence(BaseModel):
    """The band of stated confidence a competent analyst could defend on this case.

    Both ends matter. Too low is a failure to commit where the evidence is plain; too high is
    overclaiming. A single target number could not express either.
    """

    model_config = ConfigDict(frozen=True)

    lowest: float = Field(ge=0.0, le=1.0)
    highest: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> "DefensibleConfidence":
        if self.lowest > self.highest:
            raise ValueError("the lowest defensible confidence cannot exceed the highest")
        return self

    def contains(self, probability: float) -> bool:
        """Whether a stated confidence falls inside the defensible band."""
        return self.lowest <= probability <= self.highest

    def overclaimed(self, probability: float) -> bool:
        """Whether the analyst claimed more certainty than this case can support."""
        return probability > self.highest

    def underclaimed(self, probability: float) -> bool:
        """Whether the analyst hedged below what the evidence plainly supports."""
        return probability < self.lowest


class BenchmarkCase(BaseModel):
    """A subject paired with the conclusion a competent analyst should reach, and the trap it sets.

    An investigator is handed this case's `subject` and never the case itself. That is how ground
    truth is kept out of every prompt: the leak is prevented by the runner's signature rather than
    by remembering not to pass the wrong object.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    subject: AnySubject
    expected: Judgment
    confidence: DefensibleConfidence
    must_establish: tuple[str, ...] = ()
    trap: CaseTrap = CaseTrap.NONE
    notes: str = ""

    def kind(self) -> SubjectKind:
        """The kind of subject this case investigates."""
        return self.subject.kind

    def was_reached(self, judgment: Judgment) -> bool:
        """Whether an investigation arrived at the expected conclusion."""
        return judgment is self.expected

    def rewards_abstention(self) -> bool:
        """Whether declining to conclude is the correct answer here."""
        return self.expected is Judgment.INSUFFICIENT_EVIDENCE


class Benchmark(BaseModel):
    """The set of subjects and claims this harness is evaluated against."""

    cases: tuple[BenchmarkCase, ...] = ()

    @classmethod
    def load(cls, path: Path) -> "Benchmark":
        """Read the benchmark from its on-disk definition."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def case(self, case_id: str) -> BenchmarkCase:
        """One named case."""
        for case in self.cases:
            if case.case_id == case_id:
                return case
        raise KeyError(f"no benchmark case called {case_id!r}")

    def identifiers(self) -> tuple[str, ...]:
        """Every case id, in definition order."""
        return tuple(case.case_id for case in self.cases)

    def of_kind(self, kind: SubjectKind) -> tuple[BenchmarkCase, ...]:
        """Every case investigating a given kind of subject."""
        return tuple(case for case in self.cases if case.kind() is kind)

    def traps_covered(self) -> frozenset[CaseTrap]:
        """Which failure modes this benchmark actually exercises."""
        return frozenset(case.trap for case in self.cases)
