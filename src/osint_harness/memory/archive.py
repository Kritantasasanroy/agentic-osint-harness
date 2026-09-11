from difflib import SequenceMatcher
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from osint_harness.domain.analysis import Judgment
from osint_harness.domain.investigation import Investigation, Recollection
from osint_harness.domain.provenance import SourceReliability
from osint_harness.domain.subject import Subject, SubjectKind
from osint_harness.storage import Persisted


class RememberedInvestigation(BaseModel):
    """What one finished episode leaves behind for later ones to recall.

    Deliberately a digest of conclusions, never the evidence itself. Because the archive cannot
    hand back a document, nothing recalled from it can be cited, which is what stops long-term
    memory quietly contaminating a later investigation's citation set.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    descriptor: str = Field(min_length=1)
    kind: SubjectKind
    judgment: Judgment
    probability: float = Field(ge=0.0, le=1.0)
    key_findings: tuple[str, ...] = ()

    @classmethod
    def of(cls, investigation: Investigation) -> "RememberedInvestigation":
        """Reduce a finished investigation to what is worth carrying forward."""
        assessment = investigation.latest_assessment()
        return cls(
            run_id=investigation.run_id,
            descriptor=investigation.subject.descriptor(),
            kind=investigation.subject.kind,
            judgment=assessment.judgment,
            probability=assessment.probability,
            key_findings=investigation.findings.key_findings,
        )

    def as_recollection(self, similarity: float) -> Recollection:
        """Render this digest as something a later investigation can be told."""
        finding = self.key_findings[0] if self.key_findings else "no findings were recorded"
        return Recollection(
            text=(
                f"An earlier investigation of {self.descriptor} concluded "
                f"{self.judgment.value} (p={self.probability:.2f}). It reported: {finding}"
            ),
            from_run=self.run_id,
            about=self.descriptor,
            similarity=similarity,
        )


class InvestigationArchive(Persisted):
    """Finished investigations kept across episodes. This is what long-term memory actually is.

    Recall is lexical, matching on the subject's description. That is a deliberate choice rather
    than a shortcut: an embedding retriever that never confuses two people who share a name would
    leave the harmful-retrieval metric with nothing to detect, and same-name confusion is the
    canonical failure of memory in open-source intelligence work.
    """

    episodes: dict[str, RememberedInvestigation] = Field(default_factory=dict)

    MATCH_THRESHOLD: ClassVar[float] = 0.55

    def remember(self, investigation: Investigation) -> None:
        """Commit a finished investigation to memory."""
        remembered = RememberedInvestigation.of(investigation)
        self.episodes[remembered.run_id] = remembered

    def recall(self, subject: Subject, limit: int = 3) -> tuple[Recollection, ...]:
        """The most similar earlier episodes, as leads to check rather than facts to repeat."""
        descriptor = subject.descriptor()
        scored = [
            (self._similarity(descriptor, episode.descriptor), episode)
            for episode in self.episodes.values()
        ]
        matching = sorted(
            (pair for pair in scored if pair[0] >= self.MATCH_THRESHOLD),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return tuple(episode.as_recollection(score) for score, episode in matching[:limit])

    def _similarity(self, wanted: str, remembered: str) -> float:
        """How alike two subject descriptions look, ignoring case and spacing."""
        return SequenceMatcher(None, wanted.lower(), remembered.lower()).ratio()


class SourceRegister(Persisted):
    """Publisher reliability accumulated across episodes: the second half of long-term memory.

    Grading a publisher is work an investigation should not have to repeat from scratch each time,
    and carrying it forward is a real efficiency gain that the ablation can measure.
    """

    grades: dict[str, SourceReliability] = Field(default_factory=dict)
    reasons: dict[str, str] = Field(default_factory=dict)

    def learn_from(self, investigation: Investigation) -> None:
        """Adopt the grades an investigation actually assessed.

        An unassessed publisher carries the grade meaning "cannot be judged", which is the absence
        of information rather than a poor verdict, so it teaches the register nothing and is
        skipped. Recording it would pin every publisher at the lowest grade on first sighting.
        """
        for domain, source in investigation.source_grades.items():
            if source.reliability is SourceReliability.CANNOT_BE_JUDGED:
                continue
            self.grades[domain] = source.reliability
            self.reasons[domain] = source.reason

    def known_grades(self) -> dict[str, SourceReliability]:
        """Everything learned so far, to seed a new investigation."""
        return dict(self.grades)

    def reason_for(self, domain: str) -> str:
        """Why a publisher carries the grade it does, carried forward with the grade itself."""
        return self.reasons.get(domain, "no reason recorded")
