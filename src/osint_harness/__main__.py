import argparse
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from osint_harness.bench.case import Benchmark, BenchmarkCase
from osint_harness.bench.outcome import BenchmarkRun, CaseOutcome
from osint_harness.domain.investigation import Budget, Investigation, MemoryMode
from osint_harness.investigator import Investigator
from osint_harness.memory.archive import InvestigationArchive, SourceRegister
from osint_harness.model.client import ModelClient
from osint_harness.model.live import LiveModel
from osint_harness.rehearsal import RehearsedModel
from osint_harness.report.ablation import Ablation
from osint_harness.report.dossier import Dossier
from osint_harness.sources.cassette import Cassette, CassetteMode
from osint_harness.sources.tools import Encyclopedia, PageFetch


class Workspace:
    """Where a run's inputs and outputs live on disk."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def benchmark(self) -> Benchmark:
        """The benchmark definition shipped with the repository."""
        return Benchmark.load(self._root / "benchmark" / "cases.json")

    def cassette_path(self) -> Path:
        """The recording every run replays from."""
        return self._root / "benchmark" / "cassettes.json"

    def archive_path(self, memory_mode: MemoryMode) -> Path:
        """Long-term memory, kept per mode so the ablation's arms cannot contaminate each other."""
        return self._root / "runs" / f"archive-{memory_mode.value}.json"

    def register_path(self, memory_mode: MemoryMode) -> Path:
        """Learned publisher grades, likewise kept per mode."""
        return self._root / "runs" / f"sources-{memory_mode.value}.json"

    def forget(self, memory_mode: MemoryMode) -> None:
        """Clear a mode's long-term memory before a sweep.

        Without this the archive on disk is hidden state outside `(case, memory mode, cassette)`,
        and a second run of the same command reports different numbers because it recalls the first
        run's episodes. Memory still accumulates *within* a sweep, which is what the `long` arm is
        for; it just no longer leaks between invocations.
        """
        for path in (self.archive_path(memory_mode), self.register_path(memory_mode)):
            path.unlink(missing_ok=True)

    def run_directory(self, run_id: str) -> Path:
        """Where one episode's report and record are written."""
        directory = self._root / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def results_directory(self) -> Path:
        """Where sweep-level results are written."""
        directory = self._root / "runs" / "results"
        directory.mkdir(parents=True, exist_ok=True)
        return directory


class Harness:
    """Wires the pieces together for one command and writes what it produced."""

    def __init__(self, workspace: Workspace, live: bool, record: bool) -> None:
        self._workspace = workspace
        self._live = live
        self._record = record
        self._cassette = Cassette.load(
            workspace.cassette_path(),
            CassetteMode.RECORD if record else CassetteMode.REPLAY,
        )

    def model(self) -> ModelClient:
        """The reasoning engine, real or rehearsed."""
        return LiveModel() if self._live else RehearsedModel()

    def investigator(self, memory_mode: MemoryMode) -> Investigator:
        """An investigator wired to this mode's own memory, so the arms stay independent."""
        return Investigator(
            model=self.model(),
            encyclopedia=Encyclopedia(self._cassette),
            pages=PageFetch(self._cassette),
            archive=InvestigationArchive.read_from(self._workspace.archive_path(memory_mode)),
            register=SourceRegister.read_from(self._workspace.register_path(memory_mode)),
        )

    def investigate(
        self, case: BenchmarkCase, memory_mode: MemoryMode, budget: Budget
    ) -> Investigation:
        """Run one case and write its dossier."""
        investigator = self.investigator(memory_mode)
        investigation = investigator.investigate(
            run_id=f"{case.case_id}-{memory_mode.value}",
            subject=case.subject,
            memory_mode=memory_mode,
            budget=budget,
        )
        self._write_dossier(investigation)
        self._persist(investigator, memory_mode)
        return investigation

    def sweep(
        self, cases: tuple[BenchmarkCase, ...], memory_mode: MemoryMode, budget: Budget
    ) -> BenchmarkRun:
        """Run every case in one memory mode, keeping failures in the denominator.

        Memory is cleared first so the sweep is reproducible: the same command run twice reports
        the same numbers.
        """
        self._workspace.forget(memory_mode)
        outcomes: list[CaseOutcome] = []
        for case in cases:
            investigation = self.investigate(case, memory_mode, budget)
            outcomes.append(CaseOutcome.scored(investigation, case))
            print(f"  {case.case_id}: {outcomes[-1].reached.value} ({outcomes[-1].failure.value})")
        return BenchmarkRun(memory_mode=memory_mode, outcomes=tuple(outcomes))

    def _write_dossier(self, investigation: Investigation) -> None:
        dossier = Dossier(investigation)
        directory = self._workspace.run_directory(investigation.run_id)
        (directory / "findings.md").write_text(dossier.as_markdown(), encoding="utf-8")
        (directory / "investigation.json").write_text(dossier.as_json(), encoding="utf-8")

    def _persist(self, investigator: Investigator, memory_mode: MemoryMode) -> None:
        investigator.archive().write_to(self._workspace.archive_path(memory_mode))
        investigator.register().write_to(self._workspace.register_path(memory_mode))
        if self._record:
            self._cassette.write_to(self._workspace.cassette_path())


class Invocation(BaseModel):
    """One parsed command line, typed at the boundary rather than read off an untyped namespace."""

    command: str
    case_id: str = ""
    memory: MemoryMode = MemoryMode.SHORT
    live: bool = False
    record: bool = False
    max_steps: int = Field(default=24, gt=0)

    def budget(self) -> Budget:
        """The per-episode ceiling this invocation asked for."""
        return Budget(max_steps=self.max_steps)


class CommandLine:
    """The command surface described in the README."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    @classmethod
    def shared_flags(cls) -> argparse.ArgumentParser:
        """Flags accepted either before or after the subcommand, so neither order surprises."""
        shared = argparse.ArgumentParser(add_help=False)
        shared.add_argument(
            "--live",
            action="store_true",
            default=argparse.SUPPRESS,
            help="use the hosted model instead of the rehearsed analyst (needs an API key)",
        )
        shared.add_argument(
            "--record",
            action="store_true",
            default=argparse.SUPPRESS,
            help="allow live retrieval and write it to the cassette (otherwise replay only)",
        )
        shared.add_argument(
            "--max-steps",
            type=int,
            default=argparse.SUPPRESS,
            help="per-episode step ceiling (default 24)",
        )
        return shared

    @classmethod
    def parser(cls) -> argparse.ArgumentParser:
        """Every command and flag the harness accepts."""
        parser = argparse.ArgumentParser(
            prog="osint-harness",
            description="An agentic OSINT investigation harness with an evaluation benchmark.",
            parents=[cls.shared_flags()],
        )
        parser.set_defaults(live=False, record=False, max_steps=24, case_id="", memory="short")
        commands = parser.add_subparsers(dest="command", required=True)
        memory_choices = [mode.value for mode in MemoryMode]

        investigate = commands.add_parser(
            "investigate", parents=[cls.shared_flags()], help="run a single benchmark case"
        )
        investigate.add_argument("case_id", help="a case id from benchmark/cases.json")
        investigate.add_argument(
            "--memory", choices=memory_choices, default=argparse.SUPPRESS
        )

        bench = commands.add_parser(
            "bench", parents=[cls.shared_flags()], help="run every case in one memory mode"
        )
        bench.add_argument("--memory", choices=memory_choices, default=argparse.SUPPRESS)

        commands.add_parser(
            "ablate",
            parents=[cls.shared_flags()],
            help="run every case in all three memory modes and compare",
        )
        commands.add_parser("cases", help="list the benchmark cases")
        return parser

    @classmethod
    def invocation(cls, argv: tuple[str, ...]) -> Invocation:
        """Parse and validate a command line into a typed request."""
        return Invocation.model_validate(vars(cls.parser().parse_args(argv)))

    def run(self, argv: tuple[str, ...]) -> int:
        """Dispatch one invocation. Returns the process exit code."""
        invocation = self.invocation(argv)
        if invocation.command == "cases":
            return self._list_cases()

        harness = Harness(self._workspace, live=invocation.live, record=invocation.record)
        if invocation.command == "investigate":
            return self._investigate(
                harness, invocation.case_id, invocation.memory, invocation.budget()
            )
        if invocation.command == "bench":
            return self._bench(harness, invocation.memory, invocation.budget())
        return self._ablate(harness, invocation.budget())

    def _list_cases(self) -> int:
        for case in self._workspace.benchmark().cases:
            print(
                f"{case.case_id:<38} {case.kind().value:<8} "
                f"expects {case.expected.value:<22} trap: {case.trap.value}"
            )
        return 0

    def _investigate(
        self, harness: Harness, case_id: str, memory: MemoryMode, budget: Budget
    ) -> int:
        case = self._workspace.benchmark().case(case_id)
        investigation = harness.investigate(case, memory, budget)
        print(Dossier(investigation).as_markdown())
        print(f"Written to {self._workspace.run_directory(investigation.run_id)}")
        return 0

    def _bench(self, harness: Harness, memory: MemoryMode, budget: Budget) -> int:
        print(f"Running the benchmark with memory={memory.value}")
        run = harness.sweep(self._workspace.benchmark().cases, memory, budget)
        self._write_run(run)
        print(f"\nAccuracy {run.accuracy():.0%} · mean reward {run.mean_reward():.3f}")
        return 0

    def _ablate(self, harness: Harness, budget: Budget) -> int:
        cases = self._workspace.benchmark().cases
        runs = []
        for mode in MemoryMode:
            print(f"Running the benchmark with memory={mode.value}")
            run = harness.sweep(cases, mode, budget)
            self._write_run(run)
            runs.append(run)
        ablation = Ablation(runs=tuple(runs))
        report = ablation.as_markdown()
        (self._workspace.results_directory() / "ablation.md").write_text(report, encoding="utf-8")
        print(f"\n{report}")
        return 0

    def _write_run(self, run: BenchmarkRun) -> None:
        path = self._workspace.results_directory() / f"run-{run.memory_mode.value}.json"
        path.write_text(run.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def main(cls) -> int:
        """Entry point for the `osint-harness` command."""
        workspace = Workspace(Path(os.environ.get("OSINT_HARNESS_ROOT", Path.cwd())))
        return cls(workspace).run(tuple(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(CommandLine.main())
