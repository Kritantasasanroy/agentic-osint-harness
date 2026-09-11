from pydantic import BaseModel

from osint_harness.domain.analysis import Judgment
from osint_harness.domain.provenance import InformationCredibility, Source, SourceReliability
from osint_harness.graph.schemas import (
    AppraisalResult,
    CollectionPlan,
    DirectionPlan,
    ExtractedAssertion,
    ReadingChoice,
    ReconciliationResult,
    ReflectionResult,
    ReportDraft,
    SourceGrading,
)
from osint_harness.model.client import ModelClient, ModelUnavailableError, SearchFindings, Usage


class RehearsedModel(ModelClient):
    """A deterministic stand-in analyst, so the harness runs end to end with no API key.

    It does no reasoning. It reads the subject and the retrieved documents out of the briefing the
    phases already emit, extracts the opening sentence of each document as an assertion, and then
    declines to reach a verdict — which is the only honest output for something that cannot weigh
    evidence. Its purpose is to exercise and demonstrate the machinery, never to stand in for
    analysis, and its abstentions should not be read as the agent's real behaviour.
    """

    SUBJECT_MARKER = "SUBJECT ("
    URL_MARKER = "URL: "
    COST_PER_CALL = Usage(input_tokens=600, output_tokens=180)

    def decide[T: BaseModel](self, purpose: str, _system: str, prompt: str, schema: type[T]) -> T:
        self._charge(self.COST_PER_CALL)
        reply = self._reply_to(purpose, prompt)
        if not isinstance(reply, schema):
            raise ModelUnavailableError(
                f"the rehearsed analyst produced {type(reply).__name__} for {purpose!r}, "
                f"but {schema.__name__} was required"
            )
        return reply

    def search(self, _query: str) -> SearchFindings:
        """No web search offline. Retrieval comes from the encyclopedia, which needs no key."""
        self._charge(self.COST_PER_CALL)
        return SearchFindings()

    def _reply_to(self, purpose: str, prompt: str) -> BaseModel:
        """The fixed reply for each phase."""
        replies = {
            "direction": self._direction,
            "collection": self._collection,
            "reading_choice": self._reading_choice,
            "appraisal": self._appraisal,
            "reconciliation": self._reconciliation,
            "reflection": self._reflection,
            "dissemination": self._dissemination,
        }
        if purpose not in replies:
            raise ModelUnavailableError(f"the rehearsed analyst has no reply for {purpose!r}")
        return replies[purpose](prompt)

    def _direction(self, _prompt: str) -> DirectionPlan:
        """Offer nothing, so the phase falls back to the subject's own questions and hypotheses."""
        return DirectionPlan()

    def _collection(self, prompt: str) -> CollectionPlan:
        return CollectionPlan(encyclopedia_lookups=(self.subject_in(prompt),))

    def _reading_choice(self, _prompt: str) -> ReadingChoice:
        return ReadingChoice(reason="the rehearsed analyst opens nothing it was not handed")

    def _appraisal(self, prompt: str) -> AppraisalResult:
        urls = self.urls_in(prompt)
        return AppraisalResult(
            assertions=tuple(
                ExtractedAssertion(
                    assertion=f"A retrieved document at {url} describes the subject.",
                    document_url=url,
                    credibility=InformationCredibility.POSSIBLY_TRUE,
                    rationale="recorded by the rehearsed analyst without judgement",
                )
                for url in urls
            ),
            gradings=tuple(
                SourceGrading(
                    domain=Source.registrable_domain(url),
                    reliability=SourceReliability.FAIRLY_RELIABLE,
                    reason="graded uniformly; the rehearsed analyst does not assess publishers",
                )
                for url in urls
            ),
        )

    def _reconciliation(self, _prompt: str) -> ReconciliationResult:
        return ReconciliationResult(
            judgment=Judgment.INSUFFICIENT_EVIDENCE,
            probability=0.5,
            rationale=(
                "The rehearsed analyst does not weigh evidence, so no verdict is defensible. "
                "Run with a real model to obtain a judgment."
            ),
        )

    def _reflection(self, _prompt: str) -> ReflectionResult:
        return ReflectionResult(
            ready_to_conclude=True,
            gaps=("No reasoning was applied; every question remains genuinely open.",),
            reason="the rehearsed analyst has nothing further to add",
        )

    def _dissemination(self, prompt: str) -> ReportDraft:
        return ReportDraft(
            summary=(
                "This report was produced by the rehearsed analyst, which retrieves documents but "
                "performs no reasoning. The citations below are real retrievals; the absence of a "
                "verdict is deliberate."
            ),
            key_findings=tuple(
                f"A document was retrieved from {Source.registrable_domain(url)}."
                for url in self.urls_in(prompt)
            ),
            gaps=("No evidence was weighed, so no conclusion is offered.",),
        )

    @classmethod
    def subject_in(cls, prompt: str) -> str:
        """The subject descriptor, read out of the briefing header the phases emit."""
        for line in prompt.splitlines():
            if line.startswith(cls.SUBJECT_MARKER):
                return line.partition("): ")[2].strip()
        return ""

    @classmethod
    def urls_in(cls, prompt: str) -> tuple[str, ...]:
        """Every document URL the briefing listed, in order, without repeats."""
        found: dict[str, None] = {}
        for line in prompt.splitlines():
            if line.startswith(cls.URL_MARKER):
                found[line.removeprefix(cls.URL_MARKER).strip()] = None
        return tuple(found)
