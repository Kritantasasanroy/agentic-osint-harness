from pathlib import Path

from osint_harness.domain.analysis import Assessment, Findings, Judgment
from osint_harness.domain.investigation import Investigation, MemoryMode
from osint_harness.domain.provenance import SourceReliability
from osint_harness.domain.subject import Company, Person
from osint_harness.memory.archive import (
    InvestigationArchive,
    RememberedInvestigation,
    SourceRegister,
)


class Episode:
    """Builds finished investigations for the archive to remember."""

    @classmethod
    def concluded(
        cls,
        run_id: str,
        subject: Company | Person,
        judgment: Judgment = Judgment.SUPPORTED,
        probability: float = 0.8,
        finding: str = "The subject is an operating entity.",
    ) -> Investigation:
        investigation = Investigation.open(
            run_id=run_id, subject=subject, memory_mode=MemoryMode.LONG
        )
        investigation.assess(
            Assessment(
                judgment=judgment,
                leading_hypothesis="h",
                probability=probability,
                rationale="because",
            )
        )
        investigation.record_findings(Findings(summary="done", key_findings=(finding,)))
        return investigation


class TestRememberedInvestigation:
    def test_reduces_an_episode_to_its_conclusions(self) -> None:
        remembered = RememberedInvestigation.of(
            Episode.concluded("run-1", Company(name="Acme Corp"))
        )

        assert remembered.run_id == "run-1"
        assert remembered.judgment is Judgment.SUPPORTED
        assert remembered.key_findings == ("The subject is an operating entity.",)

    def test_carries_no_documents_so_nothing_recalled_can_be_cited(self) -> None:
        investigation = Episode.concluded("run-1", Company(name="Acme Corp"))
        remembered = RememberedInvestigation.of(investigation)

        assert "url" not in remembered.model_dump_json().lower()
        assert not hasattr(remembered, "evidence")


class TestArchiveStorage:
    def test_round_trip_preserves_what_was_remembered(self, tmp_path: Path) -> None:
        archive = InvestigationArchive()
        archive.remember(Episode.concluded("run-1", Company(name="Acme Corp")))
        path = tmp_path / "nested" / "archive.json"
        archive.write_to(path)

        assert InvestigationArchive.read_from(path).episodes["run-1"].descriptor == "Acme Corp"

    def test_loading_a_missing_archive_starts_empty(self, tmp_path: Path) -> None:
        assert InvestigationArchive.read_from(tmp_path / "absent.json").episodes == {}


class TestRecall:
    def test_recalls_an_earlier_investigation_of_the_same_subject(self) -> None:
        archive = InvestigationArchive()
        archive.remember(Episode.concluded("run-1", Company(name="Acme Corp")))

        recalled = archive.recall(Company(name="Acme Corp"))

        assert len(recalled) == 1
        assert "Acme Corp" in recalled[0].text
        assert recalled[0].from_run == "run-1"

    def test_does_not_recall_an_unrelated_subject(self) -> None:
        archive = InvestigationArchive()
        archive.remember(Episode.concluded("run-1", Company(name="Zenith Industries")))

        assert archive.recall(Company(name="Acme Corp")) == ()

    def test_recall_is_a_lead_not_a_finding(self) -> None:
        archive = InvestigationArchive()
        archive.remember(Episode.concluded("run-1", Company(name="Acme Corp")))

        recalled = archive.recall(Company(name="Acme Corp"))

        assert "An earlier investigation" in recalled[0].text
        assert recalled[0].similarity > 0.0

    def test_two_people_sharing_a_name_are_confused_and_that_is_attributable(self) -> None:
        archive = InvestigationArchive()
        archive.remember(
            Episode.concluded(
                "run-1",
                Person(name="John Smith", qualifiers=("Acme Corp",)),
                judgment=Judgment.REFUTED,
                finding="This John Smith was disqualified as a director.",
            )
        )

        recalled = archive.recall(Person(name="John Smith", qualifiers=("Beta Ltd",)))

        assert len(recalled) == 1
        assert recalled[0].about == "John Smith (Acme Corp)"
        assert recalled[0].from_run == "run-1"

    def test_recall_is_capped_so_a_briefing_cannot_be_flooded(self) -> None:
        archive = InvestigationArchive()
        for index in range(6):
            archive.remember(Episode.concluded(f"run-{index}", Company(name="Acme Corp")))

        assert len(archive.recall(Company(name="Acme Corp"), limit=2)) == 2


class TestSourceRegister:
    def _graded(self, run_id: str, domain: str, grade: SourceReliability) -> Investigation:
        investigation = Episode.concluded(run_id, Company(name="Acme Corp"))
        investigation.grade_source(domain, grade, f"assessed during {run_id}")
        return investigation

    def test_learns_an_assessed_grade(self) -> None:
        register = SourceRegister()

        register.learn_from(
            self._graded("run-1", "reuters.com", SourceReliability.COMPLETELY_RELIABLE)
        )

        assert register.known_grades()["reuters.com"] is SourceReliability.COMPLETELY_RELIABLE

    def test_an_unassessed_publisher_teaches_nothing(self) -> None:
        register = SourceRegister()

        register.learn_from(
            self._graded("run-1", "unknown.example", SourceReliability.CANNOT_BE_JUDGED)
        )

        assert register.known_grades() == {}

    def test_a_later_assessment_replaces_an_earlier_one(self) -> None:
        register = SourceRegister()
        register.learn_from(
            self._graded("run-1", "blog.example", SourceReliability.FAIRLY_RELIABLE)
        )

        register.learn_from(self._graded("run-2", "blog.example", SourceReliability.UNRELIABLE))

        assert register.known_grades()["blog.example"] is SourceReliability.UNRELIABLE
        assert "run-2" in register.reasons["blog.example"]

    def test_round_trip_preserves_what_was_learned(self, tmp_path: Path) -> None:
        register = SourceRegister()
        register.learn_from(self._graded("run-1", "ft.com", SourceReliability.USUALLY_RELIABLE))
        path = tmp_path / "sources.json"
        register.write_to(path)

        assert SourceRegister.read_from(path).known_grades()["ft.com"] is (
            SourceReliability.USUALLY_RELIABLE
        )
