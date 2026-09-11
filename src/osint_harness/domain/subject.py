from abc import abstractmethod
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class SubjectKind(StrEnum):
    """The category of thing an investigation is about."""

    COMPANY = "company"
    PERSON = "person"
    CLAIM = "claim"


class Subject(BaseModel):
    """The entity or proposition an investigation is about."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    qualifiers: tuple[str, ...] = ()

    @abstractmethod
    def seed_leads(self) -> tuple[str, ...]:
        """The opening lines of enquiry appropriate to this kind of subject."""

    @abstractmethod
    def opening_hypotheses(self) -> tuple[str, ...]:
        """Competing answers to state before any evidence is gathered."""

    def descriptor(self) -> str:
        """The subject rendered as a single searchable phrase."""
        if not self.qualifiers:
            return self.name
        return f"{self.name} ({', '.join(self.qualifiers)})"


class Company(Subject):
    """A legally registered trading entity."""

    kind: Literal[SubjectKind.COMPANY] = SubjectKind.COMPANY
    jurisdiction: str = ""

    def seed_leads(self) -> tuple[str, ...]:
        return (
            f"Does {self.name} exist as a registered legal entity, and in which jurisdiction?",
            f"Who founded, owns, or leads {self.name}?",
            f"What does {self.name} actually sell, and to whom?",
            f"What funding, revenue, or ownership change was most recently reported "
            f"at {self.name}?",
            f"Is there adverse coverage, litigation, or regulatory action involving {self.name}?",
        )

    def opening_hypotheses(self) -> tuple[str, ...]:
        return (
            f"{self.name} is an operating entity whose public description is broadly accurate, "
            "with no material adverse findings.",
            f"{self.name} is an operating entity, but its public description is materially "
            "inaccurate or there are adverse findings against it.",
            f"{self.name} does not exist as described, or the name refers to a different entity "
            "than the one intended.",
        )


class Person(Subject):
    """An individual human being, identified by name and disambiguating context."""

    kind: Literal[SubjectKind.PERSON] = SubjectKind.PERSON
    affiliation: str = ""

    def seed_leads(self) -> tuple[str, ...]:
        return (
            f"Which specific individual named {self.name} is meant, and what distinguishes them "
            "from others sharing that name?",
            f"What is the documented professional history of {self.name}?",
            f"What public statements or published work is attributable to {self.name}?",
            f"Is there adverse coverage, litigation, or regulatory action involving {self.name}?",
        )

    def opening_hypotheses(self) -> tuple[str, ...]:
        return (
            f"The public record for {self.name} refers to one identifiable individual and is "
            "broadly accurate.",
            f"The public record for {self.name} conflates two or more distinct individuals "
            "sharing that name.",
            f"There is no substantive public record for the {self.name} intended here.",
        )


class Claim(Subject):
    """A specific proposition asserted as fact, which can be supported or refuted."""

    kind: Literal[SubjectKind.CLAIM] = SubjectKind.CLAIM
    proposition: str = Field(min_length=1)
    asserted_about: str = ""

    def seed_leads(self) -> tuple[str, ...]:
        return (
            f"What is the original, primary source for the assertion: {self.proposition}?",
            f"Do independent sources corroborate that {self.proposition}?",
            f"Do any credible sources contradict that {self.proposition}?",
            "Does the assertion rest on a premise that is itself false or undefined?",
        )

    def opening_hypotheses(self) -> tuple[str, ...]:
        return (
            f"The assertion is accurate as stated: {self.proposition}",
            f"The assertion is inaccurate as stated: {self.proposition}",
            "The assertion is partly accurate but materially misleading as stated.",
            "The assertion rests on a false or undefined premise and cannot be evaluated as put.",
        )


AnySubject = Annotated[Company | Person | Claim, Field(discriminator="kind")]
