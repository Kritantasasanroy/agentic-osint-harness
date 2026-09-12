from osint_harness.domain.investigation import (
    Budget,
    Investigation,
    InvestigationPhase,
    MemoryMode,
    Recollection,
    Step,
)
from osint_harness.domain.subject import AnySubject
from osint_harness.graph.machine import InvestigationGraph, Node
from osint_harness.graph.phases import (
    Appraisal,
    Collection,
    Direction,
    Dissemination,
    Reconciliation,
    Reflection,
)
from osint_harness.memory.archive import InvestigationArchive, SourceRegister
from osint_harness.model.client import ModelClient, ModelRefusedError, ModelUnavailableError, Usage
from osint_harness.sources.tools import Tool


class Investigator:
    """Assembles the machine for one episode, runs it, and commits what it learned to memory.

    It takes a `Subject` and never a `BenchmarkCase`. That signature is the ground-truth leak
    guard: an expected answer cannot reach a prompt, because the object carrying it never reaches
    this class at all.
    """

    def __init__(
        self,
        *,
        model: ModelClient,
        encyclopedia: Tool,
        pages: Tool,
        web_search: Tool,
        archive: InvestigationArchive,
        register: SourceRegister,
    ) -> None:
        self._model = model
        self._encyclopedia = encyclopedia
        self._pages = pages
        self._web_search = web_search
        self._archive = archive
        self._register = register

    def investigate(
        self,
        run_id: str,
        subject: AnySubject,
        memory_mode: MemoryMode,
        budget: Budget | None = None,
    ) -> Investigation:
        """Run one episode end to end and return the finished investigation."""
        investigation = Investigation.open(
            run_id=run_id,
            subject=subject,
            memory_mode=memory_mode,
            budget=budget,
            recalled_priors=self._priors(subject, memory_mode),
        )
        self._seed_source_grades(investigation)
        try:
            self.machine().run(investigation)
        except (ModelUnavailableError, ModelRefusedError) as failure:
            self._halt_on_failure(investigation, failure)
        self._archive.remember(investigation)
        self._register.learn_from(investigation)
        return investigation

    def _halt_on_failure(
        self, investigation: Investigation, failure: ModelUnavailableError | ModelRefusedError
    ) -> None:
        """Convert a model failure into a visible halt instead of losing the episode outright.

        A crashed episode must stay in the benchmark denominator with a tag, not disappear — the
        graph engine deliberately does not catch a node's own exception (a bug in a node should
        never be silently absorbed), so this outer boundary is where a real model/network failure
        is turned into the same kind of halt budget exhaustion already produces.

        The halting step also claims whatever the failing call itself spent. `LiveModel` charges
        usage as soon as a response is parsed, before validation can reject malformed or truncated
        content — a real, billed call that happened to fail is not the same as a free one, and an
        earlier version of this method left it uncounted by defaulting the step's tokens to zero.
        """
        spent = self._model.spent()
        already_recorded = Usage(
            input_tokens=sum(step.input_tokens for step in investigation.steps),
            output_tokens=sum(step.output_tokens for step in investigation.steps),
        )
        unattributed = spent.since(already_recorded)
        investigation.record_step(
            Step(
                index=len(investigation.steps),
                phase=investigation.phase,
                moved_to=InvestigationPhase.HALTED,
                reason=f"halted: {failure}",
                input_tokens=unattributed.input_tokens,
                output_tokens=unattributed.output_tokens,
            )
        )
        investigation.phase = InvestigationPhase.HALTED

    def archive(self) -> InvestigationArchive:
        """The long-term memory this investigator writes to, so a caller can persist it."""
        return self._archive

    def register(self) -> SourceRegister:
        """The publisher grades this investigator has learned, so a caller can persist them."""
        return self._register

    def machine(self) -> InvestigationGraph:
        """The wired state machine. Exposed so a caller can inspect what phases exist."""
        nodes: dict[InvestigationPhase, Node] = {
            InvestigationPhase.DIRECTION: Direction(self._model),
            InvestigationPhase.COLLECTION: Collection(
                self._model, self._encyclopedia, self._pages, self._web_search
            ),
            InvestigationPhase.APPRAISAL: Appraisal(self._model),
            InvestigationPhase.RECONCILIATION: Reconciliation(self._model),
            InvestigationPhase.REFLECTION: Reflection(self._model),
            InvestigationPhase.DISSEMINATION: Dissemination(self._model),
        }
        return InvestigationGraph(nodes)

    def _priors(self, subject: AnySubject, memory_mode: MemoryMode) -> tuple[Recollection, ...]:
        """What earlier episodes have to say, but only when the mode permits recall."""
        if not memory_mode.recalls_past_episodes():
            return ()
        return self._archive.recall(subject)

    def _seed_source_grades(self, investigation: Investigation) -> None:
        """Carry learned publisher grades into an episode that is allowed to remember them."""
        if not investigation.memory_mode.recalls_past_episodes():
            return
        for domain, grade in self._register.known_grades().items():
            investigation.grade_source(
                domain, grade, f"recalled: {self._register.reason_for(domain)}"
            )
