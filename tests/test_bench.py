from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from osint_harness.bench.case import Benchmark, BenchmarkCase, CaseTrap, DefensibleConfidence
from osint_harness.domain.analysis import Judgment
from osint_harness.domain.investigation import InvestigationPhase, MemoryMode
from osint_harness.domain.provenance import Document, InformationCredibility, SourceReliability
from osint_harness.domain.subject import Company, SubjectKind
from osint_harness.graph.schemas import (
    AppraisalResult,
    CollectionPlan,
    DirectionPlan,
    ExtractedAssertion,
    ReconciliationResult,
    ReflectionResult,
    ReportDraft,
    SourceGrading,
)
from osint_harness.investigator import Investigator
from osint_harness.memory.archive import InvestigationArchive, SourceRegister
from osint_harness.model.client import ModelClient, ModelUnavailableError, ScriptedModel, Usage
from osint_harness.sources.cassette import Cassette, CassetteMode
from osint_harness.sources.tools import Tool


class Fixtures:
    """Loads the real benchmark and builds a fully scripted investigator to run it against."""

    ARTICLE = "https://reuters.com/acme-files-accounts"

    @classmethod
    def benchmark_path(cls) -> Path:
        return Path(__file__).resolve().parent.parent / "benchmark" / "cases.json"

    @classmethod
    def benchmark(cls) -> Benchmark:
        return Benchmark.load(cls.benchmark_path())

    @classmethod
    def document(cls) -> Document:
        return Document.retrieved(
            url=cls.ARTICLE,
            title="A retrieved article",
            text="The subject filed accounts for 2024.",
            retrieved_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

    @classmethod
    def model(cls) -> ScriptedModel:
        model = ScriptedModel()
        model.script("direction", DirectionPlan(hypotheses=("It operates.", "It is dormant.")))
        model.script("collection", CollectionPlan(encyclopedia_lookups=("the subject",)))
        model.script(
            "appraisal",
            AppraisalResult(
                assertions=(
                    ExtractedAssertion(
                        assertion="The subject filed accounts for 2024.",
                        document_url=cls.ARTICLE,
                        credibility=InformationCredibility.CONFIRMED,
                    ),
                ),
                gradings=(
                    SourceGrading(
                        domain="reuters.com",
                        reliability=SourceReliability.COMPLETELY_RELIABLE,
                        reason="wire service",
                    ),
                ),
            ),
        )
        model.script(
            "reconciliation",
            ReconciliationResult(judgment=Judgment.SUPPORTED, probability=0.8),
        )
        model.script("reflection", ReflectionResult(ready_to_conclude=True))
        model.script(
            "dissemination",
            ReportDraft(summary="The subject operates.", key_findings=("It filed accounts.",)),
        )
        return model

    @classmethod
    def investigator(
        cls,
        model: ScriptedModel,
        archive: InvestigationArchive | None = None,
        register: SourceRegister | None = None,
    ) -> Investigator:
        return Investigator(
            model=model,
            encyclopedia=SingleDocumentSource((cls.document(),)),
            pages=SingleDocumentSource(()),
            web_search=SingleDocumentSource(()),
            archive=archive if archive is not None else InvestigationArchive(),
            register=register if register is not None else SourceRegister(),
        )


class SingleDocumentSource(Tool):
    """A source returning fixed documents, so an episode can run with no network."""

    def __init__(self, documents: tuple[Document, ...]) -> None:
        super().__init__(Cassette(mode=CassetteMode.RECORD))
        self._documents = documents

    @property
    def name(self) -> str:
        return "stub_source"

    def retrieve(self, _query: str) -> tuple[Document, ...]:
        return self._documents


class TestDefensibleConfidence:
    def test_a_band_must_be_ordered(self) -> None:
        with pytest.raises(ValidationError):
            DefensibleConfidence(lowest=0.9, highest=0.2)

    def test_recognises_a_confidence_inside_the_band(self) -> None:
        band = DefensibleConfidence(lowest=0.6, highest=0.9)

        assert band.contains(0.75)
        assert not band.overclaimed(0.75)
        assert not band.underclaimed(0.75)

    def test_distinguishes_overclaiming_from_hedging(self) -> None:
        band = DefensibleConfidence(lowest=0.6, highest=0.9)

        assert band.overclaimed(0.95)
        assert band.underclaimed(0.3)


class TestBenchmarkDefinition:
    def test_the_shipped_benchmark_loads(self) -> None:
        assert len(Fixtures.benchmark().cases) >= 12

    def test_every_subject_kind_is_represented(self) -> None:
        benchmark = Fixtures.benchmark()

        for kind in SubjectKind:
            assert benchmark.of_kind(kind), f"no cases investigate a {kind.value}"

    def test_the_three_traps_the_brief_demands_are_all_present(self) -> None:
        covered = Fixtures.benchmark().traps_covered()

        assert CaseTrap.INSUFFICIENT_EVIDENCE in covered
        assert CaseTrap.SAME_NAME in covered
        assert CaseTrap.FALSE_PREMISE in covered

    def test_every_judgment_including_abstention_is_exercised(self) -> None:
        expected = {case.expected for case in Fixtures.benchmark().cases}

        assert expected == set(Judgment)

    def test_case_identifiers_are_unique(self) -> None:
        identifiers = Fixtures.benchmark().identifiers()

        assert len(identifiers) == len(set(identifiers))

    def test_abstention_cases_are_scored_for_hedging_not_certainty(self) -> None:
        for case in Fixtures.benchmark().cases:
            if case.rewards_abstention():
                assert case.confidence.highest <= 0.8, case.case_id

    def test_an_unknown_case_is_an_error_not_a_silent_miss(self) -> None:
        with pytest.raises(KeyError, match="no benchmark case"):
            Fixtures.benchmark().case("not-a-case")


class TestGroundTruthContainment:
    def test_the_expected_answer_never_reaches_a_prompt(self) -> None:
        case = Fixtures.benchmark().case("apollo-11-date-claim")
        model = Fixtures.model()

        Fixtures.investigator(model).investigate(
            run_id="leak-check", subject=case.subject, memory_mode=MemoryMode.SHORT
        )

        seen = "\n".join(model.prompts_seen).lower()
        assert seen
        for requirement in case.must_establish:
            assert requirement.lower() not in seen, f"leaked: {requirement}"
        assert case.notes.lower() not in seen
        assert case.trap.value not in seen
        assert case.case_id not in seen

    def test_the_investigator_cannot_be_handed_a_case_at_all(self) -> None:
        signature = Investigator.investigate.__annotations__

        assert "subject" in signature
        assert "BenchmarkCase" not in str(signature)


class FailingModel(ModelClient):
    """A model that always fails, so a mid-episode crash can be exercised without a real network."""

    def decide[T: BaseModel](
        self, purpose: str, _system: str, _prompt: str, _schema: type[T]
    ) -> T:
        raise ModelUnavailableError(f"simulated outage during {purpose}")


class FailsAfterChargingModel(ModelClient):
    """Mimics `LiveModel`'s real failure shape: usage is charged as soon as a reply is parsed,
    before validation can reject it, so a billed call that failed is not a free one."""

    COST_PER_CALL = Usage(input_tokens=500, output_tokens=200)

    def decide[T: BaseModel](
        self, purpose: str, _system: str, _prompt: str, _schema: type[T]
    ) -> T:
        self._charge(self.COST_PER_CALL)
        raise ModelUnavailableError(f"simulated malformed reply during {purpose}")


class SucceedsOnceThenFailsModel(ModelClient):
    """Answers Direction for real, then fails on the next call — charging first, like the live
    client does — so the halt's token attribution can be tested past the very first call, where a
    bug that double-counted or dropped already-recorded steps would otherwise hide."""

    FIRST_CALL_COST = Usage(input_tokens=300, output_tokens=100)
    FAILING_CALL_COST = Usage(input_tokens=500, output_tokens=200)

    def __init__(self) -> None:
        super().__init__()
        self._calls = 0

    def decide[T: BaseModel](
        self, purpose: str, _system: str, _prompt: str, schema: type[T]
    ) -> T:
        self._calls += 1
        if self._calls == 1:
            self._charge(self.FIRST_CALL_COST)
            reply = DirectionPlan(hypotheses=("It operates.", "It is dormant."))
            assert isinstance(reply, schema)
            return reply
        self._charge(self.FAILING_CALL_COST)
        raise ModelUnavailableError(f"simulated malformed reply during {purpose}")


class TestInvestigatorSurvivesAModelFailure:
    """The live path can genuinely fail now (network, malformed replies, rate limits). One bad
    episode must halt with a tag and stay scoreable, not raise and take an entire sweep down with
    it — the guideline that a crashed episode stays in the denominator only holds if the crash is
    actually caught somewhere."""

    def _investigator(self) -> Investigator:
        return Investigator(
            model=FailingModel(),
            encyclopedia=SingleDocumentSource(()),
            pages=SingleDocumentSource(()),
            web_search=SingleDocumentSource(()),
            archive=InvestigationArchive(),
            register=SourceRegister(),
        )

    def test_a_model_failure_mid_episode_halts_rather_than_raising(self) -> None:
        investigation = self._investigator().investigate(
            run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )

        assert investigation.phase is InvestigationPhase.HALTED
        assert "halted" in investigation.steps[-1].reason

    def test_a_halted_episode_is_still_committed_to_memory(self) -> None:
        archive = InvestigationArchive()
        register = SourceRegister()
        Investigator(
            model=FailingModel(),
            encyclopedia=SingleDocumentSource(()),
            pages=SingleDocumentSource(()),
            web_search=SingleDocumentSource(()),
            archive=archive,
            register=register,
        ).investigate(run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT)

        assert "r1" in archive.episodes

    def test_the_halt_claims_whatever_the_failing_call_itself_spent(self) -> None:
        """An independent audit found the first version of this halt recorded zero tokens for a
        call that had genuinely been charged before it failed — exactly `LiveModel`'s real shape,
        where usage is charged as soon as a reply is parsed, before validation can reject it."""
        model = FailsAfterChargingModel()

        investigation = Investigator(
            model=model,
            encyclopedia=SingleDocumentSource(()),
            pages=SingleDocumentSource(()),
            web_search=SingleDocumentSource(()),
            archive=InvestigationArchive(),
            register=SourceRegister(),
        ).investigate(run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT)

        assert investigation.phase is InvestigationPhase.HALTED
        halted_step = investigation.steps[-1]
        assert halted_step.input_tokens == FailsAfterChargingModel.COST_PER_CALL.input_tokens
        assert halted_step.output_tokens == FailsAfterChargingModel.COST_PER_CALL.output_tokens
        assert investigation.token_cost() == model.spent().total()

    def test_the_halt_attributes_only_the_failing_calls_own_spend_not_earlier_steps_too(
        self,
    ) -> None:
        """The fix reads cumulative spend minus what earlier successful steps already claimed.
        Proven only by a failure on a call that is NOT the first, where a version that summed the
        whole cumulative total into the halt step would double-count Direction's own tokens."""
        model = SucceedsOnceThenFailsModel()

        investigation = Investigator(
            model=model,
            encyclopedia=SingleDocumentSource(()),
            pages=SingleDocumentSource(()),
            web_search=SingleDocumentSource(()),
            archive=InvestigationArchive(),
            register=SourceRegister(),
        ).investigate(run_id="r1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT)

        assert investigation.phase is InvestigationPhase.HALTED
        direction_step, halted_step = investigation.steps[0], investigation.steps[-1]
        assert direction_step.input_tokens == (
            SucceedsOnceThenFailsModel.FIRST_CALL_COST.input_tokens
        )
        assert halted_step.input_tokens == SucceedsOnceThenFailsModel.FAILING_CALL_COST.input_tokens
        assert halted_step.output_tokens == (
            SucceedsOnceThenFailsModel.FAILING_CALL_COST.output_tokens
        )
        assert investigation.token_cost() == model.spent().total()


class TestInvestigator:
    def test_runs_a_case_end_to_end_with_no_network(self) -> None:
        case = Fixtures.benchmark().case("anthropic-company")

        investigation = Fixtures.investigator(Fixtures.model()).investigate(
            run_id="run-1", subject=case.subject, memory_mode=MemoryMode.SHORT
        )

        assert investigation.phase is InvestigationPhase.COMPLETE
        assert investigation.findings.is_written()
        assert investigation.ungrounded_citations() == ()

    def test_a_finished_episode_is_committed_to_memory(self) -> None:
        archive = InvestigationArchive()
        register = SourceRegister()

        Fixtures.investigator(Fixtures.model(), archive, register).investigate(
            run_id="run-1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.SHORT
        )

        assert "run-1" in archive.episodes
        assert register.known_grades()["reuters.com"] is SourceReliability.COMPLETELY_RELIABLE

    def test_long_memory_carries_a_prior_into_the_next_episode(self) -> None:
        archive = InvestigationArchive()
        register = SourceRegister()
        subject = Company(name="Acme Corp")
        Fixtures.investigator(Fixtures.model(), archive, register).investigate(
            run_id="run-1", subject=subject, memory_mode=MemoryMode.LONG
        )

        second = Fixtures.investigator(Fixtures.model(), archive, register).investigate(
            run_id="run-2", subject=subject, memory_mode=MemoryMode.LONG
        )

        assert second.recalled_priors
        assert second.recalled_priors[0].from_run == "run-1"

    def test_short_memory_recalls_nothing_from_earlier_episodes(self) -> None:
        archive = InvestigationArchive()
        register = SourceRegister()
        subject = Company(name="Acme Corp")
        Fixtures.investigator(Fixtures.model(), archive, register).investigate(
            run_id="run-1", subject=subject, memory_mode=MemoryMode.SHORT
        )

        second = Fixtures.investigator(Fixtures.model(), archive, register).investigate(
            run_id="run-2", subject=subject, memory_mode=MemoryMode.SHORT
        )

        assert second.recalled_priors == ()

    def test_long_memory_seeds_publisher_grades_learned_earlier(self) -> None:
        register = SourceRegister()
        archive = InvestigationArchive()
        Fixtures.investigator(Fixtures.model(), archive, register).investigate(
            run_id="run-1", subject=Company(name="Acme Corp"), memory_mode=MemoryMode.LONG
        )

        second = Fixtures.investigator(Fixtures.model(), archive, register).investigate(
            run_id="run-2", subject=Company(name="Beta Ltd"), memory_mode=MemoryMode.LONG
        )

        assert second.reliability_of("reuters.com") is SourceReliability.COMPLETELY_RELIABLE

    def test_the_wired_machine_covers_every_non_terminal_phase(self) -> None:
        machine = Fixtures.investigator(Fixtures.model()).machine()

        assert machine.wired_phases()


class TestBenchmarkCaseBehaviour:
    def test_recognises_the_expected_conclusion(self) -> None:
        case = BenchmarkCase(
            case_id="c",
            subject=Company(name="Acme"),
            expected=Judgment.REFUTED,
            confidence=DefensibleConfidence(lowest=0.6, highest=0.9),
        )

        assert case.was_reached(Judgment.REFUTED)
        assert not case.was_reached(Judgment.SUPPORTED)

    def test_knows_when_abstention_is_the_right_answer(self) -> None:
        case = BenchmarkCase(
            case_id="c",
            subject=Company(name="Acme"),
            expected=Judgment.INSUFFICIENT_EVIDENCE,
            confidence=DefensibleConfidence(lowest=0.2, highest=0.6),
        )

        assert case.rewards_abstention()
