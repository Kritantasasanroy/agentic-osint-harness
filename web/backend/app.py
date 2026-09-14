"""HTTP surface for the hosted demo.

Every investigation this serves is real: a live model, live search, live retrieval. There is no
rehearsed or replayed stand-in anywhere on this path, and none of its types can represent one — see
`InvestigateRequest` and `_carry_out` below. Three problems that follows from, which the CLI never
has to solve. A public endpoint sits in front of a metered API key, so live calls get claimed from a
shared daily allowance rather than trusted to whoever is calling, and a request the allowance cannot
honour is refused outright rather than quietly downgraded to something else. A real multi-phase
investigation against a live model takes minutes, far longer than a browser or the proxy in front of
this service will hold one connection open, so every investigation runs as a background job that the
page polls while it works. And the one exception on this whole surface, the memory ablation, is
deliberately never live at all: comparing memory modes is only valid when every arm sees
byte-identical retrieval, so it always replays the same recorded cassette the CLI itself is audited
against, which is a controlled measurement, not a dummy stand-in for the real thing.

A visitor can also upload a document to have it checked. Its text is read on arrival, the live model
picks out the one factual claim in it worth checking, and that claim is investigated exactly like a
benchmark claim: against sources found independently, never against the document itself, because a
document cannot corroborate itself. There is no expected answer to score an upload against, so its
result is a verdict and a dossier rather than a `CaseOutcome`. The list of a visitor's documents
lives in their browser rather than here, because this free instance forgets everything it holds
whenever it goes to sleep.
"""

import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, model_validator

from documents import DocumentClaim, Intake, SubmittedDocument, UnreadableDocumentError, Verdict
from osint_harness.__main__ import Harness, Workspace
from osint_harness.bench.case import BenchmarkCase
from osint_harness.bench.outcome import CaseOutcome
from osint_harness.domain.investigation import Budget, Investigation, MemoryMode
from osint_harness.investigator import Investigator
from osint_harness.memory.archive import InvestigationArchive, SourceRegister
from osint_harness.model.client import ModelClient, ModelUnavailableError, Usage
from osint_harness.model.live import DEFAULT_MODEL, LiveModel
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


class JobKind(StrEnum):
    """What a background job is investigating."""

    CASE = "case"
    DOCUMENT = "document"


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


class JobProgress(BaseModel):
    """How far one background job has got, as a polling browser sees it."""

    id: str
    state: JobState
    phase: str
    calls_made: int
    elapsed_seconds: float
    error: str = ""
    record: Investigation | None = None
    markdown: str = ""

    @model_validator(mode="after")
    def _a_failure_says_what_went_wrong(self) -> "JobProgress":
        if self.state is JobState.FAILED and not self.error:
            raise ValueError("a failed job must say what went wrong")
        return self


class CaseJobView(JobProgress):
    """A benchmark case under investigation, scored against its expected answer once finished."""

    kind: Literal[JobKind.CASE] = JobKind.CASE
    outcome: CaseOutcome | None = None

    @model_validator(mode="after")
    def _payload_matches_state(self) -> "CaseJobView":
        """A finished job carries its result, and an unfinished one cannot pretend to.

        `state` and these nullable fields would otherwise encode the same fact twice, leaving
        `DONE` with no outcome representable. This is the response model the browser consumes, and
        the page dereferences the outcome directly once it sees a terminal state, so the invariant
        is load-bearing on the consumer and belongs in the type rather than in a comment.
        """
        finished = self.state is JobState.DONE
        if finished and (self.outcome is None or self.record is None):
            raise ValueError("a finished job must carry its outcome and record")
        if not finished and (self.outcome is not None or self.record is not None):
            raise ValueError("only a finished job may carry an outcome or record")
        return self


class DocumentJobView(JobProgress):
    """An uploaded document being checked: the claim read from it, then the verdict on it."""

    kind: Literal[JobKind.DOCUMENT] = JobKind.DOCUMENT
    document: SubmittedDocument
    claim: DocumentClaim | None = None
    verdict: Verdict | None = None

    @model_validator(mode="after")
    def _payload_matches_state(self) -> "DocumentJobView":
        """A finished check says what claim it read, and carries a verdict exactly when that claim
        could be checked.

        A document with nothing checkable in it finishes too, with no verdict and no record,
        because declining to check an opinion column is a result rather than a failure. Anything
        short of finished carries neither, whether or not its claim has been read yet.
        """
        concluded = self.verdict is not None or self.record is not None
        if self.state is not JobState.DONE:
            if concluded:
                raise ValueError("only a finished check may carry a verdict or record")
            return self
        if self.claim is None:
            raise ValueError("a finished check must say what claim it read")
        if self.claim.is_checkable() and (self.verdict is None or self.record is None):
            raise ValueError("a finished check of a checkable claim must carry its verdict")
        if not self.claim.is_checkable() and concluded:
            raise ValueError("a check with nothing to check cannot carry a verdict")
        return self


JobView = Annotated[CaseJobView | DocumentJobView, Field(discriminator="kind")]


class Started(BaseModel):
    """The handle a caller polls after asking for an investigation."""

    job_id: str


class InvestigateRequest(BaseModel):
    """One case to investigate live, under one memory mode.

    There is no `live` flag here to turn off. Every investigation this starts is real: a live
    model, live search, live retrieval. A request this demo cannot honour live (no key configured,
    or today's allowance spent) is refused outright by `HostedDemo.start`, not silently downgraded
    to a rehearsed stand-in that never reasons.
    """

    case_id: str = Field(min_length=1)
    memory: MemoryMode = MemoryMode.SHORT


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


class Job(ABC):
    """One piece of live work running in the background, readable while it is still running.

    The phase it reports comes from the purpose of each model call, which is why a wrapper around
    the model client is what updates it: that is the one place every phase already passes through,
    so progress needs no change to the graph engine itself.
    """

    def __init__(self, job_id: str) -> None:
        self._lock = threading.Lock()
        self._id = job_id
        self._state = JobState.RUNNING
        self._phase = "starting"
        self._calls = 0
        self._started = time.monotonic()
        self._error = ""
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

    def failed(self, error: str) -> None:
        """Record that the work could not be produced at all."""
        with self._lock:
            self._state = JobState.FAILED
            self._error = error

    def _finish(self, record: Investigation | None, markdown: str) -> None:
        """Mark the job done with what it produced. The caller already holds the lock."""
        self._state = JobState.DONE
        self._phase = "complete"
        self._record = record
        self._markdown = markdown

    def _elapsed_seconds(self) -> float:
        return round(time.monotonic() - self._started, 1)

    @abstractmethod
    def view(self) -> CaseJobView | DocumentJobView:
        """A consistent snapshot, taken under the lock so a poll never reads a half-written job."""


class CaseJob(Job):
    """A benchmark case being investigated in the background."""

    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        self._outcome: CaseOutcome | None = None

    def succeeded(self, outcome: CaseOutcome, record: Investigation, markdown: str) -> None:
        """Record the finished investigation."""
        with self._lock:
            self._outcome = outcome
            self._finish(record, markdown)

    def view(self) -> CaseJobView:
        with self._lock:
            return CaseJobView(
                id=self._id,
                state=self._state,
                phase=self._phase,
                calls_made=self._calls,
                elapsed_seconds=self._elapsed_seconds(),
                error=self._error,
                outcome=self._outcome,
                record=self._record,
                markdown=self._markdown,
            )


class DocumentJob(Job):
    """An uploaded document being checked in the background."""

    def __init__(self, job_id: str, document: SubmittedDocument) -> None:
        super().__init__(job_id)
        self._document = document
        self._claim: DocumentClaim | None = None
        self._verdict: Verdict | None = None

    def document(self) -> SubmittedDocument:
        """The document this job is checking."""
        return self._document

    def read(self, claim: DocumentClaim) -> None:
        """Record the claim the intake found, while the check of it is still to come."""
        with self._lock:
            self._claim = claim

    def checked(self, verdict: Verdict, record: Investigation, markdown: str) -> None:
        """Record the finished check of the claim."""
        with self._lock:
            self._verdict = verdict
            self._finish(record, markdown)

    def found_nothing_to_check(self, markdown: str) -> None:
        """Record that the document holds no claim worth investigating."""
        with self._lock:
            self._finish(None, markdown)

    def view(self) -> DocumentJobView:
        with self._lock:
            return DocumentJobView(
                id=self._id,
                state=self._state,
                phase=self._phase,
                calls_made=self._calls,
                elapsed_seconds=self._elapsed_seconds(),
                error=self._error,
                document=self._document,
                claim=self._claim,
                verdict=self._verdict,
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
        """Whether either provider has a key present, which decides if live mode can be offered."""
        return bool(os.environ.get("NVIDIA_API_KEY") or os.environ.get("OPENROUTER_API_KEY"))

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
        """Queue a live investigation, or refuse the request outright if one cannot be honoured.

        There is no degraded mode to fall back to here. A request this demo cannot run live is
        rejected with a clear reason before any job exists, rather than started and left to fail,
        or quietly satisfied some other way.
        """
        benchmark = self._workspace.benchmark()
        if request.case_id not in benchmark.identifiers():
            raise HTTPException(status_code=404, detail=f"no such case: {request.case_id}")
        case = benchmark.case(request.case_id)
        self._refuse_unless_live(visitor)

        job = CaseJob(job_id=uuid.uuid4().hex[:12])
        self._remember(job)
        self._runner.submit(self._carry_out, job, case, request.memory, visitor)
        return Started(job_id=job.identifier())

    def check_document(self, filename: str, content: bytes, visitor: str) -> Started:
        """Queue a live check of an uploaded document's main claim.

        A file that cannot be read is refused first, whether or not live calls are available, so a
        visitor learns their upload was the problem rather than the demo's allowance.
        """
        try:
            document = SubmittedDocument.read(filename, content)
        except UnreadableDocumentError as failure:
            raise HTTPException(status_code=422, detail=str(failure)) from failure
        self._refuse_unless_live(visitor)

        job = DocumentJob(job_id=uuid.uuid4().hex[:12], document=document)
        self._remember(job)
        self._runner.submit(self._check, job, visitor)
        return Started(job_id=job.identifier())

    def job(self, job_id: str) -> Job:
        """One job by its handle."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
        return job

    def ablation(self) -> AblationResponse:
        """Compare all three memory arms, always replayed, in a workspace of its own.

        Two things this must not do, learned the hard way from an audit. It runs through
        `Harness.sweep`, the same code path the CLI uses, rather than a second sweep loop written
        here: an earlier version hand-rolled one that never persisted the archive between cases, so
        the `long` arm recalled nothing and reported 0% harmful retrieval where the CLI reports 14%,
        publishing a flat contradiction of the project's own headline result.

        And it gets its own workspace, because a sweep clears long-term memory before each arm while
        the demo's shared archive is exactly what visitors' live investigations accumulate into. One
        visitor asking for an ablation must not wipe everyone's memory, and a sweep must not pick up
        an investigation that happened to finish midway through it.

        Never live, and that is the design rather than a saving: the comparison is only valid when
        every arm sees byte-identical retrieval, so running it against a live model would measure
        the web changing between arms rather than memory.
        """
        workspace = _workspace()
        harness = Harness(workspace, live=False, record=False, model="replay")
        cases = workspace.benchmark().cases
        runs = [harness.sweep(cases, mode, Budget()) for mode in MemoryMode]
        return AblationResponse(markdown=Ablation(runs=tuple(runs)).as_markdown())

    def _refuse_unless_live(self, visitor: str) -> None:
        """Refuse, before any job exists, work this demo cannot carry out live right now."""
        if not self.live_is_configured():
            raise HTTPException(
                status_code=503, detail="no API key is configured on this instance"
            )
        if self._allowance.remaining() <= 0:
            raise HTTPException(
                status_code=503,
                detail="today's shared allowance of live calls is spent; it resets at midnight UTC",
            )
        if self._allowance.remaining_for(visitor) <= 0:
            raise HTTPException(
                status_code=503,
                detail="you have used your share of today's live calls; it resets at midnight UTC",
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
        self, job: CaseJob, case: BenchmarkCase, memory: MemoryMode, visitor: str
    ) -> None:
        """Run one live investigation to completion, recording whatever it produced.

        Every piece here is real: `CassetteMode.RECORD` lets search and page fetches reach the
        actual network, and `HostedModel` wraps the real `LiveModel`, never a rehearsed stand-in.
        """
        try:
            cassette = Cassette.load(self._workspace.cassette_path(), CassetteMode.RECORD)
            model: ModelClient = HostedModel(
                LiveModel(model=self._model_id), self._allowance, visitor, job
            )
            investigator = self._investigator(model, cassette, memory)
            investigation = investigator.investigate(
                run_id=f"{case.case_id}-{memory.value}-{job.identifier()}",
                subject=case.subject,
                memory_mode=memory,
                budget=Budget(max_steps=self.LIVE_STEP_CEILING),
            )
            investigator.archive().write_to(self._workspace.archive_path(memory))
            investigator.register().write_to(self._workspace.register_path(memory))
            job.succeeded(
                outcome=CaseOutcome.scored(investigation, case),
                record=investigation,
                markdown=Dossier(investigation).as_markdown(),
            )
        except Exception as failure:
            logging.exception("investigation %s failed", job.identifier())
            job.failed(f"{type(failure).__name__}: {failure}")

    def _check(self, job: DocumentJob, visitor: str) -> None:
        """Read the one claim out of an uploaded document, then investigate it live.

        The investigation runs with short memory and nothing recalled or kept: a visitor's
        document is theirs, so its check neither draws on nor feeds the archive that benchmark
        investigations share. The claim goes in as a subject like any benchmark claim, which is
        what keeps the document itself out of the evidence.
        """
        try:
            document = job.document()
            model: ModelClient = HostedModel(
                LiveModel(model=self._model_id), self._allowance, visitor, job
            )
            claim = Intake(model).claim_of(document)
            job.read(claim)
            subject = claim.subject(document)
            if subject is None:
                job.found_nothing_to_check(claim.refusal_report(document))
                return
            investigator = Investigator(
                model=model,
                encyclopedia=Encyclopedia(
                    cassette := Cassette.load(
                        self._workspace.cassette_path(), CassetteMode.RECORD
                    )
                ),
                pages=PageFetch(cassette),
                web_search=WebSearch(cassette),
                archive=InvestigationArchive(),
                register=SourceRegister(),
            )
            investigation = investigator.investigate(
                run_id=f"document-{job.identifier()}",
                subject=subject,
                memory_mode=MemoryMode.SHORT,
                budget=Budget(max_steps=self.LIVE_STEP_CEILING),
            )
            job.checked(
                verdict=Verdict.of(investigation),
                record=investigation,
                markdown=Dossier(investigation).as_markdown(),
            )
        except Exception as failure:
            logging.exception("document check %s failed", job.identifier())
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


async def _upload_of(request: Request) -> bytes:
    """The raw request body, refused the moment it passes the size limit rather than once read."""
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > SubmittedDocument.MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"documents are limited to {SubmittedDocument.MAX_BYTES:,} bytes",
            )
    return bytes(received)


DEMO = HostedDemo(
    workspace=_workspace(),
    allowance=LiveAllowance(
        daily=int(os.environ.get("LIVE_CALLS_PER_DAY", "40")),
        per_visitor=int(os.environ.get("LIVE_CALLS_PER_VISITOR", "14")),
    ),
    model_id=os.environ.get("LIVE_MODEL", DEFAULT_MODEL),
)

app = FastAPI(
    title="Agentic OSINT Harness",
    description="An autonomous OSINT investigator, its benchmark, and its memory ablation.",
    version="1.1.0",
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


@app.post("/api/documents")
async def check_document(
    http_request: Request, filename: Annotated[str, Query(min_length=1, max_length=255)]
) -> Started:
    """Check the main claim in one document, sent as the raw request body, and hand back a handle.

    Reading a PDF is real work, so it runs off the event loop: polls from every other visitor keep
    being answered while one upload is parsed.
    """
    content = await _upload_of(http_request)
    return await run_in_threadpool(
        DEMO.check_document, filename, content, _visitor_of(http_request)
    )


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> JobView:
    """How one investigation or document check is getting on, and its dossier once it finishes."""
    return DEMO.job(job_id).view()


@app.get("/api/ablation")
def ablation() -> AblationResponse:
    """Run every case across all three memory modes and compare them."""
    return DEMO.ablation()
