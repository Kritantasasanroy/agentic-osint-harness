from pydantic import BaseModel

from osint_harness.bench.outcome import BenchmarkRun, FailureMode
from osint_harness.domain.investigation import MemoryMode


class Ablation(BaseModel):
    """The same benchmark run under each memory setting: the experiment the brief actually asks for.

    The comparison is only meaningful because every mode replays byte-identical retrieval from the
    cassettes. Without that, a difference between modes could just as easily be the web changing
    between runs.
    """

    runs: tuple[BenchmarkRun, ...] = ()

    def run_for(self, mode: MemoryMode) -> BenchmarkRun:
        """The sweep carried out under one memory setting."""
        for run in self.runs:
            if run.memory_mode is mode:
                return run
        raise KeyError(f"the ablation holds no run for memory mode {mode.value!r}")

    def modes(self) -> tuple[MemoryMode, ...]:
        """Which memory settings were actually run."""
        return tuple(run.memory_mode for run in self.runs)

    def as_markdown(self) -> str:
        """The comparison table, plus the failure breakdown that explains it."""
        return "\n\n".join(
            [
                "## Memory ablation",
                self._preamble(),
                self._headline_table(),
                self._efficiency_table(),
                self._reward_progression_table(),
                self._confidence_progression_table(),
                self._memory_table(),
                self._failure_table(),
            ]
        )

    def _preamble(self) -> str:
        return (
            "Every mode saw byte-identical retrieval, replayed from the same cassettes, so a "
            "difference between rows is attributable to memory rather than to the web changing "
            "between runs. `none` withholds the working state between phases, `short` carries it "
            "within the episode, and `long` adds recall of earlier investigations."
        )

    def _headline_table(self) -> str:
        header = [
            "### Accuracy, calibration and reward",
            "",
            "| Memory | Accuracy | Mean reward | Mean Brier | Defensible confidence | "
            "Abstention accuracy |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        rows = [
            f"| `{run.memory_mode.value}` | {run.accuracy():.0%} | {run.mean_reward():.3f} | "
            f"{run.mean_brier():.3f} | {run.defensible_confidence_rate():.0%} | "
            f"{run.abstention_accuracy():.0%} |"
            for run in self.runs
        ]
        return "\n".join(header + rows)

    def _efficiency_table(self) -> str:
        header = [
            "### Cost and convergence",
            "",
            "| Memory | Total tokens | Reward per 1k tokens | Mean steps | Mean tool calls | "
            "Failed tool calls | Mean assessments to a stable verdict | Mean verdict changes |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        rows = [
            f"| `{run.memory_mode.value}` | {run.total_tokens():,} | "
            f"{run.reward_per_thousand_tokens():.4f} | {run.mean_steps():.2f} | "
            f"{run.mean_tool_calls():.2f} | {run.failed_tool_calls():,} | "
            f"{run.mean_assessments_to_stable_verdict():.2f} | {run.mean_verdict_changes():.2f} |"
            for run in self.runs
        ]
        note = [
            "",
            "In offline runs the token figures are synthetic estimates from the rehearsed analyst "
            "(fixed tokens per call), not provider-measured usage. Mean steps and tool calls show "
            "what each arm spent to reach its verdict, and failed tool calls separate a source "
            "outage from a reasoning failure; wall-clock latency is left out because rehearsed "
            "timings vary between runs and would break the report's reproducibility.",
        ]
        return "\n".join(header + rows + note)

    def _reward_progression_table(self) -> str:
        header = [
            "### Reward progression",
            "",
            "Mean cumulative step reward (evidence and retrieval credit less token and call cost) "
            "after each step, which shows whether an arm earns its reward early or keeps paying "
            "for steps that add nothing; an episode that stopped earlier holds its final value, "
            "and a dash means no episode in that mode ran that long.",
            "",
        ]
        progressions = [run.mean_reward_progression() for run in self.runs]
        return "\n".join(header + self._progression_rows("Step", progressions, digits=3))

    def _confidence_progression_table(self) -> str:
        header = [
            "### Confidence progression",
            "",
            "Mean stated probability after each successive assessment, starting from the 0.50 "
            "baseline every episode opens with, which shows whether confidence moves as evidence "
            "arrives or is asserted once and left; an episode with fewer assessments holds its "
            "last value, and a dash means no episode in that mode assessed that often.",
            "",
        ]
        progressions = [run.mean_confidence_progression() for run in self.runs]
        return "\n".join(header + self._progression_rows("Assessment", progressions, digits=2))

    def _progression_rows(
        self, label: str, progressions: list[tuple[float, ...]], digits: int
    ) -> list[str]:
        rows = [
            f"| {label} | " + " | ".join(f"`{run.memory_mode.value}`" for run in self.runs) + " |",
            "| --- | " + " | ".join("---" for _ in self.runs) + " |",
        ]
        for position in range(max((len(values) for values in progressions), default=0)):
            cells = [
                f"{values[position]:.{digits}f}" if position < len(values) else "—"
                for values in progressions
            ]
            rows.append(f"| {position + 1} | " + " | ".join(cells) + " |")
        return rows

    def _memory_table(self) -> str:
        header = [
            "### Retrieval quality",
            "",
            "Irrelevant retrieval counts episodes handed a prior about a different subject. "
            "Harmful retrieval counts those where that coincided with a wrong conclusion — a "
            "correlation, not a demonstrated cause.",
            "",
            "| Memory | Irrelevant retrieval | Harmful retrieval |",
            "| --- | --- | --- |",
        ]
        rows = [
            f"| `{run.memory_mode.value}` | {run.irrelevant_retrieval_rate():.0%} | "
            f"{run.harmful_retrieval_rate():.0%} |"
            for run in self.runs
        ]
        return "\n".join(header + rows)

    def _failure_table(self) -> str:
        modes = self.modes()
        header = [
            "### Where investigations broke down",
            "",
            "One tag per episode, assigned by fixed priority. Episodes that crashed or halted stay "
            "in the denominator.",
            "",
            "| Failure | " + " | ".join(f"`{mode.value}`" for mode in modes) + " |",
            "| --- | " + " | ".join("---" for _ in modes) + " |",
        ]
        rows = []
        for failure in FailureMode:
            counts = [self.run_for(mode).failure_counts().get(failure, 0) for mode in modes]
            rows.append(
                f"| {failure.value.replace('_', ' ')} | "
                + " | ".join(str(count) for count in counts)
                + " |"
            )
        return "\n".join(header + rows)
