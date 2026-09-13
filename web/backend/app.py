"""HTTP surface for the hosted demo.

Two problems the CLI never has to solve. A public endpoint sits in front of a metered API key, so
live calls get claimed from a shared daily allowance rather than trusted to whoever is calling. And
a real multi-phase investigation against a live model takes minutes, far longer than a browser or
the proxy in front of this service will hold one connection open, so every investigation runs as a
background job that the page polls while it works.
"""

import os
import shutil
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from osint_harness.__main__ import Workspace
from osint_harness.bench.case import BenchmarkCase
from osint_harness.bench.outcome import BenchmarkRun, CaseOutcome
from osint_harness.domain.investigation import Budget, Investigation, MemoryMode
from osint_harness.investigator import Investigator
from osint_harness.memory.archive import InvestigationArchive, SourceRegister
from osint_harness.model.client import ModelClient, ModelUnavailableError, Usage
from osint_harness.model.live import DEFAULT_MODEL, LiveModel
from osint_harness.rehearsal import RehearsedModel
from osint_harness.report.ablation import Ablation
from osint_harness.report.dossier import Dossier
from osint_harness.sources.cassette import Cassette, CassetteMode
from osint_harness.sources.tools import Encyclopedia, PageFetch, WebSearch

APP_DIR = Path(__file__).resolve().parent


class JobState(StrEnum):
    """Where one background investigation has got to."""

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class LiveAllowance:
    """How many live model calls the demo may still make today, in total and per visitor.

    A public endpoint sits in front of a metered key, so both ceilings are enforced here rather
    than trusted to callers. The total protects the account's free-tier day; the per-visitor share
    stops one caller taking all of it in a single sitting. Both reset on the UTC date rolling over,
    which is how the provider counts its own day.
    """

    def __init__(self, daily: int, per_visitor: int) -> None:
        self._daily = daily
        self._per_visitor = per_visitor
        self._lock = threading.Lock()
        self._day = datetime.now(UTC).date()
        self._used = 0
        self._by_visitor: dict[str, int] = {}

    def _roll_over(self) -> None:
        today = datetime.now(UTC).date()
        if today != self._day:
            self._day = today
            self._used = 0
            self._by_visitor.clear()

    def remaining(self) -> int:
        """Calls left in today's shared allowance."""
        with self._lock:
            self._roll_over()
            return max(0, self._daily - self._used)

    def remaining_for(self, visitor: str) -> int:
        """Calls this visitor may still claim today, never more than the shared remainder."""
        with self._lock:
            self._roll_over()
            mine = max(0, self._per_visitor - self._by_visitor.get(visitor, 0))
            return min(mine, max(0, self._daily - self._used))

    def claim(self, visitor: str) -> bool:
        """Take one call for this visitor, if both ceilings still allow it."""
        with self._lock:
            self._roll_over()
            if self._used >= self._daily:
                return False
            if self._by_visitor.get(visitor, 0) >= self._per_visitor:
                return False
            self._used += 1
            self._by_visitor[visitor] = self._by_visitor.get(visitor, 0) + 1
            return True


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


class JobView(BaseModel):
    """One job as a polling browser sees it, whether it is still running or finished."""

    id: str
    state: JobState
    live: bool
    phase: str
    calls_made: int
    elapsed_seconds: float
    note: str = ""
    error: str = ""
    outcome: CaseOutcome | None = None
    record: Investigation | None = None
    markdown: str = ""


class Started(BaseModel):
    """The handle a caller polls after asking for an investigation."""

    job_id: str
    live: bool
    note: str = ""


class InvestigateRequest(BaseModel):
    """One case to investigate, under one memory mode, live or replayed."""

    case_id: str = Field(min_length=1)
    memory: MemoryMode = MemoryMode.SHORT
    live: bool = True


class AblationResponse(BaseModel):
    """The three memory arms compared, recomputed rather than quoted."""

    markdown: str


class Health(BaseModel):
    """Enough to tell a deployed instance is wired to a real benchmark and a real key."""

    status: str
    cases: int
    live_available: bool
    live_calls_remaining: int
    model: str


class Job:
    """One investigation running in the background, readable while it is still running.

    The phase it reports comes from the purpose of each model call, which is why a wrapper around
    the model client is what updates it: that is the one place every phase already passes through,
    so progress needs no change to the graph engine itself.
    """

    def __init__(self, job_id: str, live: bool) -> None:
        self._lock = threading.Lock()
        self._id = job_id
        self._live = live
        self._state = JobState.RUNNING
        self._phase = "starting"
        self._calls = 0
        self._started = time.monotonic()
        self._note = ""
        self._error = ""
        self._outcome: CaseOutcome | None = None
        self._record: Investigation | None = None
        self._markdown = ""

    def identifier(self) -> str:
        """The handle a caller polls this job by."""
        return self._id

    def began(self, purpose: str) -> None:
        """Record that a model call for this purpose just started."""
        with self._lock:
            self._phase = purpose
            self._calls += 1

    def noted(self, note: str) -> None:
        """Attach something the caller should know about how this ran."""
        with self._lock:
            self._note = note

    def succeeded(self, outcome: CaseOutcome, record: Investigation, markdown: str) -> None:
        """Record the finished investigation."""
        with self._lock:
            self._state = JobState.DONE
            self._phase = "complete"
            self._outcome = outcome
            self._record = record
            self._markdown = markdown

    def failed(self, error: str) -> None:
        """Record that the investigation could not be produced at all."""
        with self._lock:
            self._state = JobState.FAILED
            self._error = error

    def view(self) -> JobView:
        """A consistent snapshot, taken under the lock so a poll never reads a half-written job."""
        with self._lock:
            return JobView(
                id=self._id,
                state=self._state,
                live=self._live,
                phase=self._phase,
                calls_made=self._calls,
                elapsed_seconds=round(time.monotonic() - self._started, 1),
                note=self._note,
                error=self._error,
                outcome=self._outcome,
                record=self._record,
                markdown=self._markdown,
            )


class HostedModel(ModelClient):
    """A model client as the hosted demo must use it.

    Two duties the CLI has no need of. Every call is claimed from the shared allowance before it is
    made, so a public endpoint cannot drain a metered key. And the purpose of each call is reported
    to the job as it starts, so a polling browser shows which phase is running instead of a blank
    spinner. Running out mid-investigation raises `ModelUnavailableError`, which the investigator
    already turns into a recorded halt rather than a crash, so the visitor still gets a dossier
    describing how far it got.
    """

    def __init__(
        self, inner: ModelClient, allowance: LiveAllowance, visitor: str, job: Job
    ) -> None:
        super().__init__()
        self._inner = inner
        self._allowance = allowance
        self._visitor = visitor
        self._job = job

    def spent(self) -> Usage:
        """The wrapped client's own metering, not a second tally that could drift from it."""
        return self._inner.spent()

    def decide[T: BaseModel](self, purpose: str, system: str, prompt: str, schema: type[T]) -> T:
        """Claim an allowance, report the phase, then let the real client answer."""
        if not self._allowance.claim(self._visitor):
            raise ModelUnavailableError(
                "the demo's daily allowance of live model calls is spent; it resets at midnight UTC"
            )
        self._job.began(purpose)
        return self._inner.decide(purpose, system, prompt, schema)


class HostedDemo:
    """Everything the hosted demo owns: a workspace, a live allowance, and the jobs in flight.

    The workspace is process-wide rather than per-request on purpose. Long-term memory is only
    worth demonstrating if it actually accumulates across investigations, which means the archive
    has to outlive one HTTP call.
    """

    MAX_REMEMBERED_JOBS = 60
    LIVE_STEP_CEILING = 12

    def __init__(self, workspace: Workspace, allowance: LiveAllowance, model_id: str) -> None:
        self._workspace = workspace
        self._allowance = allowance
        self._model_id = model_id
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._jobs_lock = threading.Lock()
        self._runner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="investigation")

    def live_is_configured(self) -> bool:
        """Whether a key is present at all, which decides if live mode can even be offered."""
        return bool(os.environ.get("OPENROUTER_API_KEY"))

    def health(self) -> Health:
        """Liveness, proof the benchmark loaded, and whether live runs are actually available."""
        return Health(
            status="ok",
            cases=len(self._workspace.benchmark().cases),
            live_available=self.live_is_configured(),
            live_calls_remaining=self._allowance.remaining(),
            model=self._model_id,
        )

    def cases(self) -> list[CaseSummary]:
        """Every benchmark subject and claim the demo can investigate."""
        return [CaseSummary.of(case) for case in self._workspace.benchmark().cases]

    def start(self, request: InvestigateRequest, visitor: str) -> Started:
        """Queue an investigation, degrading to replay when a live run cannot be honoured."""
        benchmark = self._workspace.benchmark()
        if request.case_id not in benchmark.identifiers():
            raise HTTPException(status_code=404, detail=f"no such case: {request.case_id}")
        case = benchmark.case(request.case_id)

        live, note = self._live_decision(request.live, visitor)
        job = Job(job_id=uuid.uuid4().hex[:12], live=live)
        if note:
            job.noted(note)
        self._remember(job)
        self._runner.submit(self._carry_out, job, case, request.memory, live, visitor)
        return Started(job_id=job.identifier(), live=live, note=note)

    def _live_decision(self, wanted: bool, visitor: str) -> tuple[bool, str]:
        """Whether this run can be live, and what to tell the caller when it cannot."""
        if not wanted:
            return (False, "")
        if not self.live_is_configured():
            return (False, "No API key is configured on this instance, so this ran offline.")
        if self._allowance.remaining() <= 0:
            return (
                False,
                "The demo's shared daily allowance of live calls is spent, so this ran offline. "
                "It resets at midnight UTC.",
            )
        if self._allowance.remaining_for(visitor) <= 0:
            return (
                False,
                "You have used your share of today's live calls, so this ran offline. "
                "It resets at midnight UTC.",
            )
        return (True, "")

    def job(self, job_id: str) -> Job:
        """One job by its handle."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
        return job

    def ablation(self) -> AblationResponse:
        """Compare all three memory arms, always replayed.

        Never live, and that is the design rather than a saving: the comparison is only valid when
        every arm sees byte-identical retrieval, so running it against a live model would measure
        the web changing between arms rather than memory.
        """
        cases = self._workspace.benchmark().cases
        runs = []
        for mode in MemoryMode:
            self._workspace.forget(mode)
            outcomes = []
            for case in cases:
                investigation = self._replayed(case, mode)
                outcomes.append(CaseOutcome.scored(investigation, case))
            runs.append(BenchmarkRun(memory_mode=mode, outcomes=tuple(outcomes)))
        return AblationResponse(markdown=Ablation(runs=tuple(runs)).as_markdown())

    def _replayed(self, case: BenchmarkCase, memory: MemoryMode) -> Investigation:
        """One offline episode, used by the ablation where live runs would invalidate it."""
        cassette = Cassette.load(self._workspace.cassette_path(), CassetteMode.REPLAY)
        investigator = self._investigator(RehearsedModel(), cassette, memory)
        return investigator.investigate(
            run_id=f"{case.case_id}-{memory.value}",
            subject=case.subject,
            memory_mode=memory,
            budget=Budget(),
        )

    def _investigator(
        self, model: ModelClient, cassette: Cassette, memory: MemoryMode
    ) -> Investigator:
        """An investigator wired to this mode's own memory, so the arms stay independent."""
        return Investigator(
            model=model,
            encyclopedia=Encyclopedia(cassette),
            pages=PageFetch(cassette),
            web_search=WebSearch(cassette),
            archive=InvestigationArchive.read_from(self._workspace.archive_path(memory)),
            register=SourceRegister.read_from(self._workspace.register_path(memory)),
        )

    def _carry_out(
        self, job: Job, case: BenchmarkCase, memory: MemoryMode, live: bool, visitor: str
    ) -> None:
        """Run one investigation to completion, recording whatever it produced."""
        try:
            cassette = Cassette.load(
                self._workspace.cassette_path(),
                CassetteMode.RECORD if live else CassetteMode.REPLAY,
            )
            model: ModelClient = (
                HostedModel(LiveModel(model=self._model_id), self._allowance, visitor, job)
                if live
                else RehearsedModel()
            )
            investigator = self._investigator(model, cassette, memory)
            investigation = investigator.investigate(
                run_id=f"{case.case_id}-{memory.value}-{job.identifier()}",
                subject=case.subject,
                memory_mode=memory,
                budget=Budget(max_steps=self.LIVE_STEP_CEILING) if live else Budget(),
            )
            investigator.archive().write_to(self._workspace.archive_path(memory))
            investigator.register().write_to(self._workspace.register_path(memory))
            job.succeeded(
                outcome=CaseOutcome.scored(investigation, case),
                record=investigation,
                markdown=Dossier(investigation).as_markdown(),
            )
        except Exception as failure:
            job.failed(f"{type(failure).__name__}: {failure}")

    def _remember(self, job: Job) -> None:
        """Keep recent jobs for polling, and drop the oldest so memory stays bounded."""
        with self._jobs_lock:
            self._jobs[job.identifier()] = job
            while len(self._jobs) > self.MAX_REMEMBERED_JOBS:
                self._jobs.popitem(last=False)


def _benchmark_source() -> Path:
    """The shipped benchmark, found whether this runs from the container image or a checkout."""
    candidates = [APP_DIR / "benchmark"]
    if len(APP_DIR.parents) > 1:
        candidates.append(APP_DIR.parents[1] / "benchmark")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise RuntimeError("benchmark directory not found")


def _workspace() -> Workspace:
    """A scratch copy of the benchmark, so the demo never writes into its own container image."""
    root = Path(tempfile.mkdtemp(prefix="osint-harness-"))
    shutil.copytree(_benchmark_source(), root / "benchmark")
    return Workspace(root)


def _visitor_of(request: Request) -> str:
    """Who is calling, as far as a service behind a proxy can tell."""
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


DEMO = HostedDemo(
    workspace=_workspace(),
    allowance=LiveAllowance(
        daily=int(os.environ.get("LIVE_CALLS_PER_DAY", "40")),
        per_visitor=int(os.environ.get("LIVE_CALLS_PER_VISITOR", "14")),
    ),
    model_id=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
)

app = FastAPI(
    title="Agentic OSINT Harness",
    description="An autonomous OSINT investigator, its benchmark, and its memory ablation.",
    version="1.0.0",
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
    """Liveness, the benchmark size, and what is left of today's live allowance."""
    return DEMO.health()


@app.get("/api/cases")
def cases() -> list[CaseSummary]:
    """Every benchmark subject and claim the demo can investigate."""
    return DEMO.cases()


@app.post("/api/investigate")
def investigate(request: InvestigateRequest, http_request: Request) -> Started:
    """Queue one investigation and hand back a handle to poll."""
    return DEMO.start(request, _visitor_of(http_request))


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> JobView:
    """How one investigation is getting on, and its dossier once it finishes."""
    return DEMO.job(job_id).view()


@app.get("/api/ablation")
def ablation() -> AblationResponse:
    """Run every case across all three memory modes and compare them."""
    return DEMO.ablation()
