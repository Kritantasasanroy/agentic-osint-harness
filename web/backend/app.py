"""HTTP surface for the hosted demo.

Replay-only by construction: this module never builds a live model and never reads an API key, so
the public demo cannot spend anyone's quota or leak a credential no matter what it is asked for.
"""

import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from osint_harness.__main__ import Harness, Workspace
from osint_harness.bench.case import BenchmarkCase
from osint_harness.bench.outcome import CaseOutcome
from osint_harness.domain.investigation import Budget, Investigation, MemoryMode
from osint_harness.report.ablation import Ablation
from osint_harness.report.dossier import Dossier

APP_DIR = Path(__file__).resolve().parent


class CaseSummary(BaseModel):
    """One benchmark case, as the demo's picker lists it."""

    case_id: str
    kind: str
    subject: str
    expected: str
    trap: str
    notes: str

    @classmethod
    def of(cls, case: BenchmarkCase) -> "CaseSummary":
        """Reduce a benchmark case to what a reader choosing one actually needs."""
        return cls(
            case_id=case.case_id,
            kind=case.kind().value,
            subject=case.subject.descriptor(),
            expected=case.expected.value,
            trap=case.trap.value,
            notes=case.notes,
        )


class InvestigateRequest(BaseModel):
    """One case to investigate, under one memory mode."""

    case_id: str = Field(min_length=1)
    memory: MemoryMode = MemoryMode.SHORT


class InvestigateResponse(BaseModel):
    """A finished episode: the scored outcome, the full trajectory, and the analyst's report."""

    outcome: CaseOutcome
    record: Investigation
    markdown: str


class AblationResponse(BaseModel):
    """The three memory arms compared, recomputed rather than quoted."""

    markdown: str


class Health(BaseModel):
    """Enough to tell a deployed instance is wired to a real benchmark."""

    status: str
    mode: str
    cases: int


def _benchmark_source() -> Path:
    """The shipped benchmark, found whether this runs from the container image or a checkout."""
    for candidate in (APP_DIR / "benchmark", APP_DIR.parents[1] / "benchmark"):
        if candidate.is_dir():
            return candidate
    raise RuntimeError("benchmark directory not found")


def _workspace() -> Workspace:
    """A scratch copy of the benchmark, so a request never writes into the container image."""
    root = Path(tempfile.mkdtemp(prefix="osint-harness-"))
    shutil.copytree(_benchmark_source(), root / "benchmark")
    return Workspace(root)


def _harness(workspace: Workspace) -> Harness:
    """The harness in replay mode.

    `live` is fixed false here rather than read from configuration. A hosted demo that could be
    flipped live by an environment variable is one misconfiguration away from spending the free
    tier's whole daily allowance on its first visitor.
    """
    return Harness(workspace, live=False, record=False, model="replay")


app = FastAPI(
    title="Agentic OSINT Harness",
    description="Replay-only demo API for an autonomous OSINT investigator and its benchmark.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Send a bare visit to the generated API documentation."""
    return RedirectResponse(url="/docs")


@app.get("/api/health")
def health() -> Health:
    """Liveness, plus proof the benchmark actually loaded."""
    return Health(status="ok", mode="replay", cases=len(_workspace().benchmark().cases))


@app.get("/api/cases")
def cases() -> list[CaseSummary]:
    """Every benchmark subject and claim the demo can investigate."""
    return [CaseSummary.of(case) for case in _workspace().benchmark().cases]


@app.post("/api/investigate")
def investigate(request: InvestigateRequest) -> InvestigateResponse:
    """Run one case end to end, replayed from the recorded cassette."""
    workspace = _workspace()
    benchmark = workspace.benchmark()
    if request.case_id not in benchmark.identifiers():
        raise HTTPException(status_code=404, detail=f"no such case: {request.case_id}")

    case = benchmark.case(request.case_id)
    investigation = _harness(workspace).investigate(case, request.memory, Budget())
    return InvestigateResponse(
        outcome=CaseOutcome.scored(investigation, case),
        record=investigation,
        markdown=Dossier(investigation).as_markdown(),
    )


@app.get("/api/ablation")
def ablation() -> AblationResponse:
    """Run all three memory modes over every case and compare them."""
    workspace = _workspace()
    harness = _harness(workspace)
    cases_to_run = workspace.benchmark().cases
    runs = [harness.sweep(cases_to_run, mode, Budget()) for mode in MemoryMode]
    return AblationResponse(markdown=Ablation(runs=tuple(runs)).as_markdown())
