import pytest

from osint_harness.domain.investigation import (
    Budget,
    Investigation,
    InvestigationPhase,
    MemoryMode,
    ToolCall,
)
from osint_harness.domain.subject import Company
from osint_harness.graph.machine import InvestigationGraph, Node, Transition


class RouteTo(Node):
    """A node that always routes to a fixed phase, recording which phases it was asked from."""

    def __init__(self, transition: Transition) -> None:
        self.transition = transition
        self.seen: list[InvestigationPhase] = []

    def advance(self, investigation: Investigation) -> Transition:
        self.seen.append(investigation.phase)
        return self.transition


class TestGraphWiring:
    def test_construction_refuses_a_phase_with_no_node(self) -> None:
        with pytest.raises(ValueError, match="no node wired"):
            InvestigationGraph({})

    def test_terminal_phases_need_no_node(self) -> None:
        wired = InvestigationGraph.wired_phases()
        assert InvestigationPhase.COMPLETE not in wired
        assert InvestigationPhase.HALTED not in wired
        assert InvestigationPhase.DIRECTION in wired


class TestGraphRun:
    def _straight_line(self) -> dict[InvestigationPhase, Node]:
        order = [
            InvestigationPhase.DIRECTION,
            InvestigationPhase.COLLECTION,
            InvestigationPhase.APPRAISAL,
            InvestigationPhase.RECONCILIATION,
            InvestigationPhase.REFLECTION,
            InvestigationPhase.DISSEMINATION,
        ]
        destinations = [*order[1:], InvestigationPhase.COMPLETE]
        return {
            phase: RouteTo(Transition(next_phase=destination, reason=f"leaving {phase.value}"))
            for phase, destination in zip(order, destinations, strict=True)
        }

    def _open(self, budget: Budget | None = None) -> Investigation:
        return Investigation.open(
            run_id="r1",
            subject=Company(name="Acme Corp"),
            memory_mode=MemoryMode.SHORT,
            budget=budget,
        )

    def test_runs_every_phase_once_and_stops_at_complete(self) -> None:
        graph = InvestigationGraph(self._straight_line())
        investigation = graph.run(self._open())
        assert investigation.phase is InvestigationPhase.COMPLETE
        assert [step.phase for step in investigation.steps] == list(
            InvestigationGraph.wired_phases()
        )

    def test_records_exactly_one_step_per_transition_with_its_reason(self) -> None:
        graph = InvestigationGraph(self._straight_line())
        investigation = graph.run(self._open())
        assert len(investigation.steps) == len(InvestigationGraph.wired_phases())
        assert [step.index for step in investigation.steps] == list(range(len(investigation.steps)))
        assert investigation.steps[0].reason == "leaving direction"
        assert investigation.steps[0].moved_to is InvestigationPhase.COLLECTION

    def test_measures_latency_for_every_step(self) -> None:
        graph = InvestigationGraph(self._straight_line())
        investigation = graph.run(self._open())
        assert all(step.latency_seconds >= 0.0 for step in investigation.steps)
        assert investigation.elapsed_seconds() >= 0.0

    def test_carries_tool_calls_and_tokens_from_the_transition_onto_the_step(self) -> None:
        nodes = self._straight_line()
        nodes[InvestigationPhase.COLLECTION] = RouteTo(
            Transition(
                next_phase=InvestigationPhase.APPRAISAL,
                reason="searched",
                tool_calls=(ToolCall(tool="web_search", query="Acme Corp", documents_returned=3),),
                input_tokens=120,
                output_tokens=40,
            )
        )
        investigation = InvestigationGraph(nodes).run(self._open())
        assert len(investigation.tool_calls()) == 1
        assert investigation.tool_calls()[0].tool == "web_search"
        assert investigation.token_cost() == 160

    def test_a_node_sees_the_phase_it_was_dispatched_from(self) -> None:
        nodes = self._straight_line()
        direction = nodes[InvestigationPhase.DIRECTION]
        assert isinstance(direction, RouteTo)
        InvestigationGraph(nodes).run(self._open())
        assert direction.seen == [InvestigationPhase.DIRECTION]

    def test_an_already_terminal_investigation_runs_no_nodes(self) -> None:
        nodes = self._straight_line()
        investigation = self._open()
        investigation.phase = InvestigationPhase.COMPLETE
        InvestigationGraph(nodes).run(investigation)
        assert investigation.steps == []


class TestBudgetHalt:
    def _looping(self) -> dict[InvestigationPhase, Node]:
        return {
            phase: RouteTo(
                Transition(next_phase=InvestigationPhase.COLLECTION, reason="never converges")
            )
            if phase is InvestigationPhase.DIRECTION
            else RouteTo(
                Transition(next_phase=InvestigationPhase.DIRECTION, reason="never converges")
            )
            for phase in InvestigationGraph.wired_phases()
        }

    def test_a_non_converging_investigation_halts_instead_of_spinning(self) -> None:
        investigation = Investigation.open(
            run_id="r2",
            subject=Company(name="Acme Corp"),
            memory_mode=MemoryMode.NONE,
            budget=Budget(max_steps=4),
        )
        InvestigationGraph(self._looping()).run(investigation)
        assert investigation.phase is InvestigationPhase.HALTED

    def test_the_halt_itself_is_recorded_in_the_trajectory(self) -> None:
        investigation = Investigation.open(
            run_id="r3",
            subject=Company(name="Acme Corp"),
            memory_mode=MemoryMode.NONE,
            budget=Budget(max_steps=2),
        )
        InvestigationGraph(self._looping()).run(investigation)
        final = investigation.steps[-1]
        assert final.moved_to is InvestigationPhase.HALTED
        assert "budget exhausted" in final.reason
        assert len(investigation.steps) == 3
