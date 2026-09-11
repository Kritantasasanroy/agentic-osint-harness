from enum import StrEnum
from itertools import pairwise
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from osint_harness.domain.analysis import (
    Assessment,
    Evidence,
    Findings,
    Hypothesis,
    Judgment,
)
from osint_harness.domain.provenance import Document, Source, SourceReliability
from osint_harness.domain.subject import AnySubject


class UngroundedEvidenceError(Exception):
    """Raised when evidence is recorded against a document the investigation never retrieved."""


class MemoryMode(StrEnum):
    """How much of what the agent has learned is carried into each phase."""

    NONE = "none"
    SHORT = "short"
    LONG = "long"

    def carries_working_state(self) -> bool:
        """Whether findings accumulated in this episode survive from one phase to the next."""
        return self is not MemoryMode.NONE

    def recalls_past_episodes(self) -> bool:
        """Whether prior investigations are retrieved as priors when this one opens."""
        return self is MemoryMode.LONG


class LeadPriority(StrEnum):
    """How much an open line of enquiry should hold up a conclusion."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    def blocks_conclusion(self) -> bool:
        """Whether leaving this lead open should prevent the investigation converging."""
        return self is LeadPriority.HIGH


class LeadState(StrEnum):
    """How far a line of enquiry has been taken."""

    OPEN = "open"
    PURSUED = "pursued"
    EXHAUSTED = "exhausted"


class InvestigationPhase(StrEnum):
    """A stage of the intelligence cycle, and a state of the investigation machine."""

    DIRECTION = "direction"
    COLLECTION = "collection"
    APPRAISAL = "appraisal"
    RECONCILIATION = "reconciliation"
    REFLECTION = "reflection"
    DISSEMINATION = "dissemination"
    COMPLETE = "complete"
    HALTED = "halted"

    def is_terminal(self) -> bool:
        """Whether the machine stops here."""
        return self in (InvestigationPhase.COMPLETE, InvestigationPhase.HALTED)


class Lead(BaseModel):
    """A line of enquiry that has been identified but not yet exhausted."""

    question: str = Field(min_length=1)
    priority: LeadPriority = LeadPriority.MEDIUM
    state: LeadState = LeadState.OPEN
    origin: str = "direction"

    def pursue(self) -> None:
        """Mark that collection has acted on this lead."""
        self.state = LeadState.PURSUED

    def exhaust(self) -> None:
        """Mark that this lead has yielded everything it is going to."""
        self.state = LeadState.EXHAUSTED

    def is_open(self) -> bool:
        """Whether this lead still represents unfinished work."""
        return self.state is LeadState.OPEN


class Recollection(BaseModel):
    """Something carried in from an earlier investigation: a lead to check, never evidence.

    It keeps the episode it came from and how strongly it matched, so a recall that sent an
    investigation the wrong way can be identified afterwards rather than merely suspected.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    from_run: str = Field(min_length=1)
    about: str = ""
    similarity: float = Field(default=0.0, ge=0.0, le=1.0)


class Budget(BaseModel):
    """The ceiling on one episode: how many steps, tool calls, and tokens it may spend."""

    model_config = ConfigDict(frozen=True)

    max_steps: int = Field(default=24, gt=0)
    max_tool_calls: int = Field(default=40, gt=0)
    max_tokens: int = Field(default=200_000, gt=0)


class ToolCall(BaseModel):
    """One invocation of an external source, and what it cost."""

    model_config = ConfigDict(frozen=True)

    tool: str
    query: str
    documents_returned: int = Field(ge=0)
    succeeded: bool = True
    latency_seconds: float = Field(default=0.0, ge=0.0)
    failure_reason: str = ""


class Step(BaseModel):
    """One transition of the state machine: what ran, what it cost, and why it moved on."""

    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=0)
    phase: InvestigationPhase
    moved_to: InvestigationPhase
    reason: str
    tool_calls: tuple[ToolCall, ...] = ()
    evidence_added: tuple[str, ...] = ()
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_seconds: float = Field(default=0.0, ge=0.0)

    def tokens(self) -> int:
        """Total tokens consumed by this step."""
        return self.input_tokens + self.output_tokens

    def failed_tool_calls(self) -> tuple[ToolCall, ...]:
        """Tool invocations that did not return usable results."""
        return tuple(call for call in self.tool_calls if not call.succeeded)


class Investigation(BaseModel):
    """One episode of work on one subject, from opening question to findings report."""

    SUFFICIENT_EVIDENCE_WEIGHT: ClassVar[float] = 0.8

    run_id: str
    subject: AnySubject
    memory_mode: MemoryMode
    budget: Budget = Field(default_factory=Budget)
    phase: InvestigationPhase = InvestigationPhase.DIRECTION
    leads: list[Lead] = Field(default_factory=list)
    documents: dict[str, Document] = Field(default_factory=dict)
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    assessments: list[Assessment] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    source_grades: dict[str, Source] = Field(default_factory=dict)
    recalled_priors: tuple[Recollection, ...] = ()
    findings: Findings = Field(default_factory=Findings)

    @model_validator(mode="after")
    def _every_citation_is_grounded(self) -> "Investigation":
        """Refuse to exist holding evidence that cites a document this episode never retrieved.

        `record_evidence` already refuses one at a time, but that guards only the live path. This
        guards construction and deserialisation too, so a record cannot be assembled or reloaded
        from disk carrying a citation with nothing behind it.
        """
        unbacked = self.ungrounded_citations()
        if unbacked:
            raise ValueError(
                f"investigation {self.run_id} holds {len(unbacked)} citations with no retrieved "
                f"document behind them ({', '.join(unbacked[:3])})"
            )
        if not self.assessments:
            raise ValueError(
                f"investigation {self.run_id} has no assessment; open it with "
                "Investigation.open(), which records a baseline so the confidence series and "
                "every metric derived from it are never empty"
            )
        return self

    @classmethod
    def open(
        cls,
        run_id: str,
        subject: AnySubject,
        memory_mode: MemoryMode,
        budget: Budget | None = None,
        recalled_priors: tuple[Recollection, ...] = (),
    ) -> "Investigation":
        """Start an episode with a baseline assessment, so the confidence series is never empty."""
        return cls(
            run_id=run_id,
            subject=subject,
            memory_mode=memory_mode,
            budget=budget if budget is not None else Budget(),
            recalled_priors=recalled_priors,
            assessments=[
                Assessment(
                    judgment=Judgment.INSUFFICIENT_EVIDENCE,
                    leading_hypothesis="",
                    probability=0.5,
                    rationale="Baseline before any evidence was gathered.",
                    made_in_phase=InvestigationPhase.DIRECTION.value,
                )
            ],
        )

    def record_document(self, document: Document) -> None:
        """Store a retrieved document, registering its publisher as seen but not yet judged."""
        self.documents[document.url] = document
        if document.source_domain not in self.source_grades:
            self.source_grades[document.source_domain] = Source(domain=document.source_domain)

    def record_evidence(self, evidence: Evidence) -> str:
        """Store an extracted assertion, refusing any that does not trace to a held document."""
        if evidence.document_url not in self.documents:
            raise UngroundedEvidenceError(
                f"evidence cites {evidence.document_url}, which was never retrieved in run "
                f"{self.run_id}"
            )
        self.evidence[evidence.identifier] = evidence
        return evidence.identifier

    def grade_source(self, domain: str, reliability: SourceReliability, reason: str) -> None:
        """Grade a publisher, keeping the justification beside the grade rather than discarding it.

        A bare letter with no reason behind it is a decoration, not an assessment, so the reason is
        carried on the source and rendered in the report next to the grade it explains.
        """
        if domain not in self.source_grades:
            self.source_grades[domain] = Source(domain=domain)
        self.source_grades[domain].regrade(reliability, reason)

    def source_for(self, domain: str) -> Source:
        """The publisher record for a domain, unjudged if it has not been graded."""
        return self.source_grades.get(domain, Source(domain=domain))

    def reliability_of(self, domain: str) -> SourceReliability:
        """The grade applied to a publisher during this investigation."""
        return self.source_for(domain).reliability

    def record_findings(self, findings: Findings) -> None:
        """Attach the written report produced by dissemination."""
        self.findings = findings

    def assess(self, assessment: Assessment) -> None:
        """Append an assessment; the series is never overwritten, so progression survives."""
        self.assessments.append(assessment)

    def record_step(self, step: Step) -> None:
        """Append a step to the trajectory, which is the sole source of truth for the metrics."""
        self.steps.append(step)

    def latest_assessment(self) -> Assessment:
        """The current conclusion. Always present: an episode opens with a baseline."""
        return self.assessments[-1]

    def open_leads(self) -> tuple[Lead, ...]:
        """Lines of enquiry not yet acted on."""
        return tuple(lead for lead in self.leads if lead.is_open())

    def has_blocking_leads(self) -> bool:
        """Whether a high-priority question remains unanswered."""
        return any(lead.priority.blocks_conclusion() for lead in self.open_leads())

    def evidence_weights(self) -> dict[str, float]:
        """Each evidence item's ACH weight, from its source's grade and its own credibility."""
        return {
            identifier: item.weight(self.reliability_of(item.source_domain))
            for identifier, item in self.evidence.items()
        }

    def ranked_hypotheses(self) -> tuple[Hypothesis, ...]:
        """Hypotheses ordered by ACH: least disconfirmed first."""
        weights = self.evidence_weights()
        return tuple(sorted(self.hypotheses, key=lambda h: h.inconsistency_score(weights)))

    def total_evidence_weight(self) -> float:
        """Combined strength of everything gathered, used to decide evidential sufficiency."""
        return sum(self.evidence_weights().values())

    def has_sufficient_evidence(self) -> bool:
        """Whether what was gathered could carry a conclusive verdict at all.

        Defined once, here, because two places need it and they must not drift: the agent uses it
        to refuse a verdict it cannot support, and the evaluation uses it to tag a wrong answer as
        having rested on thin evidence. If those two thresholds disagreed, the harness would
        penalise the agent for a judgment it had itself been allowed to make.
        """
        return self.total_evidence_weight() >= self.SUFFICIENT_EVIDENCE_WEIGHT

    def verdict_changes(self) -> int:
        """How many times the judgment flipped across the assessment series."""
        return sum(
            1 for earlier, later in pairwise(self.assessments) if not earlier.agrees_with(later)
        )

    def assessments_to_stable_verdict(self) -> int:
        """How many assessments were made before the judgment stopped changing.

        This counts positions in the assessment series, not steps. The two differ: a phase that
        reaches no new conclusion appends no assessment, so an episode of six steps may hold only
        two assessments. Naming it for steps would misreport convergence.
        """
        final = self.assessments[-1].judgment
        index = len(self.assessments) - 1
        while index > 0 and self.assessments[index - 1].judgment is final:
            index -= 1
        return index

    def confidence_series(self) -> tuple[float, ...]:
        """The stated probability at each assessment, in order."""
        return tuple(assessment.probability for assessment in self.assessments)

    def tool_calls(self) -> tuple[ToolCall, ...]:
        """Every external invocation made across the episode."""
        return tuple(call for step in self.steps for call in step.tool_calls)

    def token_cost(self) -> int:
        """Total tokens consumed across the episode."""
        return sum(step.tokens() for step in self.steps)

    def elapsed_seconds(self) -> float:
        """Total wall-clock spent inside the machine."""
        return sum(step.latency_seconds for step in self.steps)

    def source_diversity(self) -> int:
        """How many distinct publishers the evidence rests on."""
        return len({item.source_domain for item in self.evidence.values()})

    def ungrounded_citations(self) -> tuple[str, ...]:
        """Cited URLs with no retrieved document behind them. Must always be empty."""
        return tuple(
            item.document_url
            for item in self.evidence.values()
            if item.document_url not in self.documents
        )

    def budget_exhausted(self) -> bool:
        """Whether any ceiling in the budget has been reached."""
        return (
            len(self.steps) >= self.budget.max_steps
            or len(self.tool_calls()) >= self.budget.max_tool_calls
            or self.token_cost() >= self.budget.max_tokens
        )
