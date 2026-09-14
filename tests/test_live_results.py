from pathlib import Path

from live_results import LiveSweep
from osint_harness.bench.case import BenchmarkCase, DefensibleConfidence
from osint_harness.bench.outcome import CaseOutcome
from osint_harness.domain.analysis import Assessment, Judgment
from osint_harness.domain.investigation import Investigation, MemoryMode
from osint_harness.domain.subject import Company


def _scored(run_id: str) -> tuple[CaseOutcome, Investigation]:
    investigation = Investigation.open(
        run_id=run_id, subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
    )
    investigation.assess(
        Assessment(
            judgment=Judgment.SUPPORTED,
            leading_hypothesis="Acme is operating.",
            probability=0.82,
            rationale="Filings corroborate the description.",
        )
    )
    case = BenchmarkCase(
        case_id="acme-company",
        subject=Company(name="Acme Corp"),
        expected=Judgment.SUPPORTED,
        confidence=DefensibleConfidence(lowest=0.6, highest=0.9),
    )
    return CaseOutcome.scored(investigation, case), investigation


class TestLiveSweepRead:
    def _write(
        self, run_dir: Path, stem: str, outcome: CaseOutcome, investigation: Investigation
    ) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / f"{stem}.outcome.json").write_text(outcome.model_dump_json(), encoding="utf-8")
        (run_dir / f"{stem}.investigation.json").write_text(
            investigation.model_dump_json(), encoding="utf-8"
        )

    def test_a_memory_mode_suffix_is_not_mistaken_for_an_attempt_number(
        self, tmp_path: Path
    ) -> None:
        """The harness itself names a run `f"{case.case_id}-{memory_mode.value}"`
        (`__main__.py`), so a case run under `short` memory leaves a file stem like
        `acme-company-short`, exactly the same shape as a second attempt's `acme-company-2`.
        Reading the non-numeric tail as an attempt number used to crash on the first, real
        sweep output rather than on some contrived edge case."""
        outcome, investigation = _scored("acme-company-short")
        self._write(tmp_path, "acme-company-short", outcome, investigation)

        sweep = LiveSweep.read(tmp_path, "test")

        assert sweep.episodes[0].attempt == 1

    def test_a_genuine_repeat_attempt_is_still_numbered(self, tmp_path: Path) -> None:
        outcome, investigation = _scored("acme-company-2")
        self._write(tmp_path, "acme-company-2", outcome, investigation)

        sweep = LiveSweep.read(tmp_path, "test")

        assert sweep.episodes[0].attempt == 2
