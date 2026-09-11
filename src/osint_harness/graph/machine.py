from abc import ABC, abstractmethod
from collections.abc import Mapping
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field

from osint_harness.domain.investigation import (
    Investigation,
    InvestigationPhase,
    Step,
    ToolCall,
)


class Transition(BaseModel):
    """What one phase decided: where the machine goes next, why, and what it spent."""

    model_config = ConfigDict(frozen=True)

    next_phase: InvestigationPhase
    reason: str = Field(min_length=1)
    tool_calls: tuple[ToolCall, ...] = ()
    evidence_added: tuple[str, ...] = ()
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class Node(ABC):
    """One state of the investigation machine, responsible for advancing it exactly one step."""

    @abstractmethod
    def advance(self, investigation: Investigation) -> Transition:
        """Do this phase's work against the investigation, and say where to go next."""


class InvestigationGraph:
    """The state machine: it routes between phases, records every step, and enforces the budget."""

    def __init__(self, nodes: Mapping[InvestigationPhase, Node]) -> None:
        missing = [phase for phase in self.wired_phases() if phase not in nodes]
        if missing:
            raise ValueError(
                f"no node wired for {', '.join(phase.value for phase in missing)}; "
                "every non-terminal phase needs one"
            )
        self._nodes = dict(nodes)

    @classmethod
    def wired_phases(cls) -> tuple[InvestigationPhase, ...]:
        """The phases a caller must supply a node for. Terminal phases run no node."""
        return tuple(phase for phase in InvestigationPhase if not phase.is_terminal())

    def run(self, investigation: Investigation) -> Investigation:
        """Advance the investigation until it concludes or exhausts its budget."""
        while not investigation.phase.is_terminal():
            if investigation.budget_exhausted():
                self._halt(investigation)
                break
            self._advance_once(investigation)
        return investigation

    def _advance_once(self, investigation: Investigation) -> None:
        phase = investigation.phase
        started = perf_counter()
        transition = self._nodes[phase].advance(investigation)
        investigation.record_step(
            Step(
                index=len(investigation.steps),
                phase=phase,
                moved_to=transition.next_phase,
                reason=transition.reason,
                tool_calls=transition.tool_calls,
                evidence_added=transition.evidence_added,
                input_tokens=transition.input_tokens,
                output_tokens=transition.output_tokens,
                latency_seconds=perf_counter() - started,
            )
        )
        investigation.phase = transition.next_phase

    def _halt(self, investigation: Investigation) -> None:
        investigation.record_step(
            Step(
                index=len(investigation.steps),
                phase=investigation.phase,
                moved_to=InvestigationPhase.HALTED,
                reason="budget exhausted before the investigation converged",
            )
        )
        investigation.phase = InvestigationPhase.HALTED
