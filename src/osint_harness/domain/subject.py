from abc import abstractmethod
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from osint_harness.domain.analysis import VerdictStandard


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

    @abstractmethod
    def proposition_under_test(self) -> str:
        """The single statement this investigation's verdict is a verdict on."""

    @abstractmethod
    def verdict_standard(self) -> VerdictStandard:
        """What each judgment asserts about this kind of subject.

        A verdict means nothing until it is clear what it is a verdict on, and for a long time no
        prompt said. Live runs on Theranos and Wirecard weighed the evidence correctly, ruling out
        the hypothesis that each company was clean and leading with the one that it had adverse
        findings, then reported `supported`, meaning that hypothesis was supported. The benchmark
        reads the same word as a verdict on the company being clean, so a correct analysis was
        scored wrong, and nothing had ever told the model which reading applied.
        """

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

    def proposition_under_test(self) -> str:
        return (
            f"{self.descriptor()} is a real entity that operates as described, with no material "
            "adverse findings against it."
        )

    def verdict_standard(self) -> VerdictStandard:
        return VerdictStandard(
            supported=(
                "it exists and operates as described, and nothing retrieved shows a material "
                "adverse finding against it"
            ),
            refuted=(
                "retrieved evidence shows a material adverse finding against it, or shows that it "
                "is materially not what it is described as, including that it no longer operates. "
                "A material adverse finding is an established fraud, a criminal conviction for "
                "conduct in the business, sanctions, insolvency or collapse, or regulatory action "
                "that stopped its core business; allegations, lawsuits, settlements, open "
                "investigations, criticism and routine fines are not"
            ),
            partially_supported=(
                "it exists and operates, but part of its description is materially wrong, short "
                "of a material adverse finding"
            ),
            insufficient_evidence=(
                "no credible record establishes that it exists or what it does, or the name cannot "
                "be tied to one specific entity; finding no record of it is not proof that it does "
                "not exist"
            ),
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
            f"The name {self.name}, together with any qualifiers given, picks out one specific, "
            "real individual, and the public record about that individual is accurate.",
            f"The name {self.name} is shared by multiple distinct real people, and nothing given, "
            "including any qualifiers, distinguishes which one is meant. Finding an abundant, "
            "internally consistent record for one prominent bearer of the name does not by "
            "itself establish that they are the one meant: a record can be extensive and still "
            "belong to the wrong person.",
            f"There is no substantive public record for anyone answering to {self.name} as "
            "described.",
        )

    def proposition_under_test(self) -> str:
        described = self.descriptor()
        if self.affiliation and self.affiliation not in self.qualifiers:
            described = f"{described}, associated with {self.affiliation}"
        return (
            f"The subject described as {described} is one specific, real individual, and what "
            "that description says about them is accurate."
        )

    def verdict_standard(self) -> VerdictStandard:
        return VerdictStandard(
            supported=(
                "the name and description pick out one specific, real individual, and what the "
                "description says about them is accurate"
            ),
            refuted=(
                "one individual can be identified, but the description is materially wrong about "
                "them"
            ),
            partially_supported=(
                "one individual can be identified, and the description is right about them in "
                "part but materially wrong in part"
            ),
            insufficient_evidence=(
                "the name and description do not pin down one individual, because the public "
                "record covers several different people who fit them, or no substantive record of "
                "such a person exists; choosing one of several people who share a name is a "
                "guess, not a finding, however much is known about the one guessed at"
            ),
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
        # ponytail: each hypothesis names the proposition mid-sentence rather than after a
        # trailing colon on purpose. A live run had a model read a colon-then-proposition suffix
        # as a label needing its own JSON key and wrapped every hypothesis in a one-entry dict,
        # which the schema (a bare string) rejected outright.
        return (
            f"The assertion that {self.proposition} is accurate in every specific detail it "
            "states.",
            f"What the assertion that {self.proposition} presupposes is real, but, setting "
            "aside any clause giving the reason, date, place, manner or actor, the core event "
            "or state of affairs that remains did not happen or does not hold at all.",
            f"What the assertion that {self.proposition} presupposes is real, and, setting "
            "aside any clause giving the reason, date, place, manner or actor, the core event "
            "or state of affairs that remains did happen or does hold, but that set-aside "
            "clause is wrong.",
            f"The assertion that {self.proposition} presupposes something that is not real, "
            "such as an office nobody currently holds, so it cannot be evaluated as true or "
            "false as put.",
        )

    def proposition_under_test(self) -> str:
        return self.proposition

    def verdict_standard(self) -> VerdictStandard:
        return VerdictStandard(
            supported=(
                "retrieved evidence establishes the proposition as stated, including every detail "
                "it attaches"
            ),
            refuted=(
                "what the proposition presupposes is real. Set aside any clause giving the "
                "reason, date, place, manner or actor; what remains is the core event. Refuted "
                "means retrieved evidence shows that core event itself did not happen or does not "
                "hold, never merely that the set-aside clause is wrong while the core event is "
                "true, that is partially supported, and never merely that what the proposition "
                "presupposes is itself unreal, that is insufficient evidence"
            ),
            partially_supported=(
                "what the proposition presupposes is real. Set aside any clause giving the "
                "reason, date, place, manner or actor; what remains is the core event, and "
                "retrieved evidence shows that core event did happen or does hold, but the "
                "set-aside clause itself is wrong. For instance, 'X did A for reason B' is "
                "partially supported, not refuted, when X did A but not for reason B, because "
                "the core event, X doing A, is true even though the reason is not; likewise 'X "
                "did A in year Y' is partially supported when X did A but not in year Y"
            ),
            insufficient_evidence=(
                "the evidence does not settle it, or the proposition presupposes something that is "
                "not real, such as an office nobody currently holds, which leaves it neither true "
                "nor false as put"
            ),
        )


AnySubject = Annotated[Company | Person | Claim, Field(discriminator="kind")]
