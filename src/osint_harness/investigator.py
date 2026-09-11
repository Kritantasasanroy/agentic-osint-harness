from osint_harness.domain.investigation import (
    Budget,
    Investigation,
    InvestigationPhase,
    MemoryMode,
    Recollection,
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
from osint_harness.model.client import ModelClient
from osint_harness.sources.tools import Tool


class Investigator:
    """Assembles the machine for one episode, runs it, and commits what it learned to memory.

    It takes a `Subject` and never a `BenchmarkCase`. That signature is the ground-truth leak
    guard: an expected answer cannot reach a prompt, because the object carrying it never reaches
    this class at all.
    """

    def __init__(
        self,
        model: ModelClient,
        encyclopedia: Tool,
        pages: Tool,
        archive: InvestigationArchive,
        register: SourceRegister,
    ) -> None:
        self._model = model
        self._encyclopedia = encyclopedia
        self._pages = pages
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
        self.machine().run(investigation)
        self._archive.remember(investigation)
        self._register.learn_from(investigation)
        return investigation

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
                self._model, self._encyclopedia, self._pages
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
