from pydantic import BaseModel, Field

from osint_harness.domain.analysis import Consistency, Judgment
from osint_harness.domain.investigation import LeadPriority
from osint_harness.domain.provenance import InformationCredibility, SourceReliability


class PlannedLead(BaseModel):
    """A line of enquiry the analyst proposes opening."""

    question: str = Field(min_length=1)
    priority: LeadPriority = LeadPriority.MEDIUM


class DirectionPlan(BaseModel):
    """What Direction decides: the questions to answer and the explanations competing to be true."""

    leads: tuple[PlannedLead, ...] = ()
    hypotheses: tuple[str, ...] = ()


class CollectionPlan(BaseModel):
    """Where Collection intends to look next."""

    search_queries: tuple[str, ...] = ()
    encyclopedia_lookups: tuple[str, ...] = ()
    urls_to_read: tuple[str, ...] = ()


class ReadingChoice(BaseModel):
    """Which of the returned search hits are worth actually opening."""

    urls: tuple[str, ...] = ()
    reason: str = ""


class ExtractedAssertion(BaseModel):
    """One claim Appraisal read out of a retrieved document."""

    assertion: str = Field(min_length=1)
    document_url: str = Field(min_length=1)
    credibility: InformationCredibility = InformationCredibility.CANNOT_BE_JUDGED
    rationale: str = ""


class SourceGrading(BaseModel):
    """Appraisal's judgment of a publisher's track record, with the reason recorded."""

    domain: str = Field(min_length=1)
    reliability: SourceReliability = SourceReliability.CANNOT_BE_JUDGED
    reason: str = ""


class AppraisalResult(BaseModel):
    """What Appraisal produced: graded assertions, and grades for the publishers behind them."""

    assertions: tuple[ExtractedAssertion, ...] = ()
    gradings: tuple[SourceGrading, ...] = ()


class ConsistencyCall(BaseModel):
    """How one piece of evidence bears on one hypothesis, by position in the ACH matrix."""

    hypothesis_index: int = Field(ge=0)
    evidence_id: str = Field(min_length=1)
    consistency: Consistency = Consistency.NOT_APPLICABLE


class ReconciliationResult(BaseModel):
    """The ACH matrix as filled in this round, and the conclusion the analyst draws from it."""

    calls: tuple[ConsistencyCall, ...] = ()
    judgment: Judgment = Judgment.INSUFFICIENT_EVIDENCE
    probability: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = ""


class ReflectionResult(BaseModel):
    """Reflection's self-critique: what is still missing, and whether it is honest to conclude."""

    gaps: tuple[str, ...] = ()
    new_leads: tuple[PlannedLead, ...] = ()
    new_hypotheses: tuple[str, ...] = ()
    ready_to_conclude: bool = False
    reason: str = ""


class ReportDraft(BaseModel):
    """The narrative findings report, written only once the evidence is in."""

    summary: str = ""
    key_findings: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
