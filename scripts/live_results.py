"""Export one live benchmark sweep as a committed results file and a markdown table.

Reads the per-case `<case>-<attempt>.outcome.json` and `.investigation.json` records a live sweep
leaves in its run directory, through the library's own `CaseOutcome`, `Investigation` and
`BenchmarkRun`, so every number is the one the harness itself scored.

    python scripts/live_results.py RUN_DIR NAME --json OUT.json --markdown OUT.md [--append]
"""

import argparse
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from osint_harness.bench.outcome import BenchmarkRun, CaseOutcome, FailureMode
from osint_harness.domain.analysis import Judgment
from osint_harness.domain.investigation import Investigation, MemoryMode


class TokenSpend(BaseModel):
    """Tokens consumed, split into what was sent to the model and what it sent back."""

    model_config = ConfigDict(frozen=True)

    prompt: int
    completion: int
    total: int

    @classmethod
    def of(cls, investigation: Investigation) -> "TokenSpend":
        """What one episode spent, read off its recorded steps."""
        return cls(
            prompt=sum(step.input_tokens for step in investigation.steps),
            completion=sum(step.output_tokens for step in investigation.steps),
            total=investigation.token_cost(),
        )


class LiveEpisode(BaseModel):
    """One scored investigation from a live sweep, reduced to what a results table carries."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    attempt: int
    expected: Judgment
    reached: Judgment
    correct: bool
    probability: float
    brier: float
    failure: FailureMode
    steps: int
    tool_calls: int
    failed_tool_calls: int
    sources: int
    evidence: int
    latency_seconds: float
    tokens: TokenSpend
    verdict_changes: int
    assessments_to_stable: int
    reward: float
    halted: bool

    @classmethod
    def of(cls, outcome: CaseOutcome, investigation: Investigation, attempt: int) -> "LiveEpisode":
        """Join an outcome to the trajectory it was scored from."""
        if outcome.run_id != investigation.run_id:
            raise ValueError(
                f"outcome {outcome.run_id} sits beside investigation {investigation.run_id}"
            )
        return cls(
            case_id=outcome.case_id,
            attempt=attempt,
            expected=outcome.expected,
            reached=outcome.reached,
            correct=outcome.correct,
            probability=outcome.probability,
            brier=round(outcome.brier, 4),
            failure=outcome.failure,
            steps=outcome.steps_taken,
            tool_calls=outcome.tool_calls,
            failed_tool_calls=sum(len(step.failed_tool_calls()) for step in investigation.steps),
            sources=outcome.source_diversity,
            evidence=outcome.evidence_count,
            latency_seconds=round(outcome.seconds, 1),
            tokens=TokenSpend.of(investigation),
            verdict_changes=outcome.verdict_changes,
            assessments_to_stable=outcome.assessments_to_stable_verdict,
            reward=round(outcome.reward.total(), 4),
            halted=outcome.halted,
        )

    @classmethod
    def label_for(cls, case_id: str, attempt: int) -> str:
        """How an episode is named in a table: the case, and the attempt only past the first."""
        return case_id if attempt == 1 else f"{case_id} (attempt {attempt})"

    def cells(self) -> tuple[object, ...]:
        """This episode as one markdown table row."""
        return (
            self.label_for(self.case_id, self.attempt),
            self.expected.value,
            self.reached.value,
            "yes" if self.correct else "**no**",
            f"{self.probability:.2f}",
            f"{self.brier:.3f}",
            self.failure.value,
            self.steps,
            self.tool_calls,
            self.failed_tool_calls,
            self.sources,
            self.evidence,
            f"{self.latency_seconds:.0f}",
            self.tokens.prompt,
            self.tokens.completion,
            self.tokens.total,
            self.verdict_changes,
            self.assessments_to_stable,
            f"{self.reward:.3f}",
        )


class SweepTotals(BaseModel):
    """What a whole sweep added up to: quality averaged, spend summed."""

    model_config = ConfigDict(frozen=True)

    cases: int
    correct: int
    accuracy: float
    mean_brier: float
    mean_reward: float
    failures: dict[FailureMode, int]
    halted: int
    steps: int
    tool_calls: int
    failed_tool_calls: int
    evidence: int
    latency_seconds: float
    tokens: TokenSpend
    mean_verdict_changes: float
    mean_assessments_to_stable: float
    crashed_without_outcome: tuple[str, ...]

    @classmethod
    def of(
        cls, run: BenchmarkRun, episodes: tuple[LiveEpisode, ...], crashed: tuple[str, ...]
    ) -> "SweepTotals":
        """Totals over every scored episode, with the run's own aggregates for the averages."""
        return cls(
            cases=len(episodes),
            correct=sum(episode.correct for episode in episodes),
            accuracy=round(run.accuracy(), 4),
            mean_brier=round(run.mean_brier(), 4),
            mean_reward=round(run.mean_reward(), 4),
            failures=run.failure_counts(),
            halted=sum(episode.halted for episode in episodes),
            steps=sum(episode.steps for episode in episodes),
            tool_calls=sum(episode.tool_calls for episode in episodes),
            failed_tool_calls=sum(episode.failed_tool_calls for episode in episodes),
            evidence=sum(episode.evidence for episode in episodes),
            latency_seconds=round(sum(outcome.seconds for outcome in run.outcomes), 1),
            tokens=TokenSpend(
                prompt=sum(episode.tokens.prompt for episode in episodes),
                completion=sum(episode.tokens.completion for episode in episodes),
                total=run.total_tokens(),
            ),
            mean_verdict_changes=round(run.mean_verdict_changes(), 4),
            mean_assessments_to_stable=round(run.mean_assessments_to_stable_verdict(), 4),
            crashed_without_outcome=crashed,
        )

    def cells(self) -> tuple[object, ...]:
        """The totals as the closing markdown table row."""
        tagged = sum(count for mode, count in self.failures.items() if mode.is_failure())
        return (
            "**Total**",
            "",
            "",
            f"{self.correct}/{self.cases}",
            "",
            f"avg {self.mean_brier:.3f}",
            f"{tagged} tagged",
            self.steps,
            self.tool_calls,
            self.failed_tool_calls,
            "",
            self.evidence,
            f"{self.latency_seconds:.0f}",
            self.tokens.prompt,
            self.tokens.completion,
            self.tokens.total,
            f"avg {self.mean_verdict_changes:.2f}",
            f"avg {self.mean_assessments_to_stable:.2f}",
            f"avg {self.mean_reward:.3f}",
        )


class SummaryRow(BaseModel):
    """One line of the live runner's summary, the only trace a crashed episode leaves."""

    CRASHED: ClassVar[str] = "crashed"

    case_id: str
    attempt: int = 1
    reached: str


class RunSummary(BaseModel):
    """The live runner's summary file, read only for the episodes that crashed before scoring."""

    FILENAME: ClassVar[str] = "summary.json"

    rows: tuple[SummaryRow, ...] = ()

    @classmethod
    def crashes_in(cls, run_dir: Path) -> tuple[SummaryRow, ...]:
        """Episodes the summary records as crashed, or none when the run left no summary."""
        path = run_dir / cls.FILENAME
        if not path.exists():
            return ()
        summary = cls.model_validate_json(path.read_text(encoding="utf-8"))
        return tuple(row for row in summary.rows if row.reached == SummaryRow.CRASHED)


class LiveSweep(BaseModel):
    """A named live benchmark sweep: every scored episode in one run directory, and its totals."""

    model_config = ConfigDict(frozen=True)

    OUTCOME_SUFFIX: ClassVar[str] = ".outcome.json"
    INVESTIGATION_SUFFIX: ClassVar[str] = ".investigation.json"
    HEADER: ClassVar[tuple[str, ...]] = (
        "Case",
        "Expected",
        "Reached",
        "Correct",
        "P",
        "Brier",
        "Failure",
        "Steps",
        "Tool calls",
        "Failed calls",
        "Sources",
        "Evidence",
        "Latency (s)",
        "Prompt tokens",
        "Completion tokens",
        "Total tokens",
        "Verdict changes",
        "Assessments to stable",
        "Reward",
    )

    name: str
    run: str
    memory_mode: MemoryMode
    reward_version: str
    totals: SweepTotals
    episodes: tuple[LiveEpisode, ...]

    @classmethod
    def read(cls, run_dir: Path, name: str) -> "LiveSweep":
        """Load every outcome in a run directory alongside the investigation it was scored from."""
        scored: list[tuple[CaseOutcome, LiveEpisode]] = []
        for path in sorted(run_dir.glob(f"*{cls.OUTCOME_SUFFIX}")):
            stem = path.name.removesuffix(cls.OUTCOME_SUFFIX)
            outcome = CaseOutcome.model_validate_json(path.read_text(encoding="utf-8"))
            investigation = Investigation.model_validate_json(
                path.with_name(stem + cls.INVESTIGATION_SUFFIX).read_text(encoding="utf-8")
            )
            suffix = stem.removeprefix(outcome.case_id).lstrip("-")
            attempt = int(suffix) if suffix.isdigit() else 1
            scored.append((outcome, LiveEpisode.of(outcome, investigation, attempt)))
        if not scored:
            raise ValueError(f"{run_dir} holds no *{cls.OUTCOME_SUFFIX} records")
        scored.sort(key=lambda pair: (pair[1].case_id, pair[1].attempt))
        outcomes = tuple(outcome for outcome, _ in scored)
        modes = {outcome.memory_mode for outcome in outcomes}
        if len(modes) != 1:
            raise ValueError(f"{run_dir} mixes memory modes {sorted(modes)}")
        episodes = tuple(episode for _, episode in scored)
        present = {(episode.case_id, episode.attempt) for episode in episodes}
        crashed = tuple(
            LiveEpisode.label_for(row.case_id, row.attempt)
            for row in RunSummary.crashes_in(run_dir)
            if (row.case_id, row.attempt) not in present
        )
        run = BenchmarkRun(memory_mode=modes.pop(), outcomes=outcomes)
        return cls(
            name=name,
            run=run_dir.resolve().name,
            memory_mode=run.memory_mode,
            reward_version=outcomes[0].reward.version,
            totals=SweepTotals.of(run, episodes, crashed),
            episodes=episodes,
        )

    @classmethod
    def row(cls, cells: tuple[object, ...]) -> str:
        """One markdown table row."""
        return "| " + " | ".join(str(cell) for cell in cells) + " |"

    def as_markdown(self) -> str:
        """The sweep as a headed markdown table closing on a totals row."""
        lines = [
            f"### {self.name}: {self.totals.correct} of {self.totals.cases} correct "
            f"(`{self.run}`, memory {self.memory_mode.value})",
            "",
            self.row(self.HEADER),
            self.row(("---",) * len(self.HEADER)),
            *(self.row(episode.cells()) for episode in self.episodes),
            self.row(self.totals.cells()),
        ]
        if self.totals.crashed_without_outcome:
            crashed = ", ".join(self.totals.crashed_without_outcome)
            lines += ["", f"Crashed before scoring, so absent above: {crashed}."]
        return "\n".join(lines) + "\n"


class LiveResultsCommandLine:
    """The command that exports one live sweep as a results file and a markdown table."""

    @classmethod
    def parser(cls) -> argparse.ArgumentParser:
        """Every argument the export accepts."""
        parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
        parser.add_argument("run_dir", type=Path, help="live sweep directory of per-case records")
        parser.add_argument("name", help="what the sweep is called in the output, e.g. holdout")
        parser.add_argument("--json", type=Path, required=True, help="results file to write")
        parser.add_argument("--markdown", type=Path, required=True, help="table file to write")
        parser.add_argument(
            "--append", action="store_true", help="append the table instead of overwriting"
        )
        return parser

    @classmethod
    def main(cls) -> int:
        """Entry point: read the sweep, write both outputs, report where they went."""
        args = cls.parser().parse_args()
        sweep = LiveSweep.read(args.run_dir, args.name)
        for path in (args.json, args.markdown):
            path.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(sweep.model_dump_json(indent=2) + "\n", encoding="utf-8")
        with args.markdown.open("a" if args.append else "w", encoding="utf-8") as table:
            table.write(sweep.as_markdown() + "\n")
        totals = sweep.totals
        print(f"{sweep.name}: {totals.correct}/{totals.cases} -> {args.json}, {args.markdown}")
        return 0


if __name__ == "__main__":
    raise SystemExit(LiveResultsCommandLine.main())
