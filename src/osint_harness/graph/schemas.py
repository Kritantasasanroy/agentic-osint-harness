from pydantic import BaseModel, Field, field_validator

from osint_harness.domain.analysis import Consistency, Judgment
from osint_harness.domain.investigation import LeadPriority
from osint_harness.domain.provenance import InformationCredibility, SourceReliability


def _flattened_hypotheses(hypotheses: object) -> object:
    """A hypothesis list recovered from a model that wrapped an entry in a one-key object.

    A live run had a model read a hypothesis containing a colon as a label needing its own JSON
    key, and wrapped it in `{"label": "the rest of the sentence"}` instead of writing it as the
    plain string the schema asks for; the whole reply then failed validation and halted the
    episode. The prompt no longer writes hypotheses with a colon in them for this reason, but this
    stays as a backstop: rejoining a one-entry object's key and value recovers the same sentence
    the model meant, at the one point a stray dict would otherwise be treated as malformed.
    """
    if not isinstance(hypotheses, (list, tuple)):
        return hypotheses
    flattened: list[object] = []
    for item in hypotheses:
        if isinstance(item, dict) and len(item) == 1:
            ((key, value),) = item.items()
            flattened.append(f"{key}: {value}")
        else:
            flattened.append(item)
    return flattened


class PlannedLead(BaseModel):
    """A line of enquiry the analyst proposes opening."""

    question: str = Field(min_length=1)
    priority: LeadPriority = LeadPriority.MEDIUM


class DirectionPlan(BaseModel):
    """What Direction decides: the questions to answer and the explanations competing to be true."""

    leads: tuple[PlannedLead, ...] = ()
    hypotheses: tuple[str, ...] = ()

    _flatten_hypotheses = field_validator("hypotheses", mode="before")(_flattened_hypotheses)


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

    domain: str = Field(
        min_length=1,
        description="A bare registrable domain such as en.wikipedia.org. No extra words.",
    )
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
    judgment: Judgment = Field(
        default=Judgment.INSUFFICIENT_EVIDENCE,
        description="A verdict on the proposition under test, as the verdict standard defines it.",
    )
    probability: float = Field(
        default=0.5, ge=0.0, le=1.0, description="The probability that the judgment is correct."
    )
    rationale: str = ""


class ReflectionResult(BaseModel):
    """Reflection's self-critique: what is still missing, and whether it is honest to conclude."""

    gaps: tuple[str, ...] = ()
    new_leads: tuple[PlannedLead, ...] = ()
    new_hypotheses: tuple[str, ...] = ()
    ready_to_conclude: bool = False
    reason: str = ""

    _flatten_new_hypotheses = field_validator("new_hypotheses", mode="before")(
        _flattened_hypotheses
    )


class ReportDraft(BaseModel):
    """The narrative findings report, written only once the evidence is in."""

    summary: str = ""
    key_findings: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
