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
            "| Memory | Total tokens | Reward per 1k tokens | Mean steps to stable verdict | "
            "Mean verdict changes |",
            "| --- | --- | --- | --- | --- |",
        ]
        rows = [
            f"| `{run.memory_mode.value}` | {run.total_tokens():,} | "
            f"{run.reward_per_thousand_tokens():.4f} | "
            f"{run.mean_steps_to_stable_verdict():.2f} | {run.mean_verdict_changes():.2f} |"
            for run in self.runs
        ]
        return "\n".join(header + rows)

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
            if not any(counts):
                continue
            rows.append(
                f"| {failure.value.replace('_', ' ')} | "
                + " | ".join(str(count) for count in counts)
                + " |"
            )
        return "\n".join(header + rows)
