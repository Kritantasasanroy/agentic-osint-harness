from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field


class SourceReliability(StrEnum):
    """Admiralty grading of a publisher's track record, independent of any one report."""

    COMPLETELY_RELIABLE = "A"
    USUALLY_RELIABLE = "B"
    FAIRLY_RELIABLE = "C"
    NOT_USUALLY_RELIABLE = "D"
    UNRELIABLE = "E"
    CANNOT_BE_JUDGED = "F"

    def weight(self) -> float:
        """How much an assertion from this publisher counts when scoring a hypothesis."""
        match self:
            case SourceReliability.COMPLETELY_RELIABLE:
                return 1.0
            case SourceReliability.USUALLY_RELIABLE:
                return 0.8
            case SourceReliability.FAIRLY_RELIABLE:
                return 0.6
            case SourceReliability.NOT_USUALLY_RELIABLE:
                return 0.4
            case SourceReliability.UNRELIABLE:
                return 0.2
            case SourceReliability.CANNOT_BE_JUDGED:
                return 0.1


class InformationCredibility(StrEnum):
    """Admiralty grading of a specific assertion, independent of who published it."""

    CONFIRMED = "1"
    PROBABLY_TRUE = "2"
    POSSIBLY_TRUE = "3"
    DOUBTFUL = "4"
    IMPROBABLE = "5"
    CANNOT_BE_JUDGED = "6"

    def weight(self) -> float:
        """How much this assertion counts when scoring a hypothesis."""
        match self:
            case InformationCredibility.CONFIRMED:
                return 1.0
            case InformationCredibility.PROBABLY_TRUE:
                return 0.8
            case InformationCredibility.POSSIBLY_TRUE:
                return 0.6
            case InformationCredibility.DOUBTFUL:
                return 0.4
            case InformationCredibility.IMPROBABLE:
                return 0.2
            case InformationCredibility.CANNOT_BE_JUDGED:
                return 0.1


class Source(BaseModel):
    """A publisher of information, existing independently of any one investigation."""

    domain: str = Field(min_length=1)
    reliability: SourceReliability = SourceReliability.CANNOT_BE_JUDGED
    reason: str = "not yet assessed"
    appearances: int = Field(default=0, ge=0)

    @classmethod
    def registrable_domain(cls, url: str) -> str:
        """The host portion of a URL, lowercased and stripped of a leading www."""
        host = urlparse(url).netloc.lower()
        return host.partition(":")[0].removeprefix("www.")

    def regrade(self, reliability: SourceReliability, reason: str) -> None:
        """Record a new reliability grade together with the justification for it."""
        self.reliability = reliability
        self.reason = reason

    def note_appearance(self) -> None:
        """Record that this publisher supplied a document in one more investigation."""
        self.appearances += 1


class Document(BaseModel):
    """A specific artifact retrieved from a source at a specific time."""

    model_config = ConfigDict(frozen=True)

    url: str = Field(min_length=1)
    title: str
    text: str
    source_domain: str = Field(min_length=1)
    retrieved_at: datetime

    @classmethod
    def retrieved(
        cls,
        url: str,
        title: str,
        text: str,
        retrieved_at: datetime | None = None,
    ) -> "Document":
        """Build a document from a retrieval, deriving its source domain from the URL."""
        return cls(
            url=url,
            title=title,
            text=text,
            source_domain=Source.registrable_domain(url),
            retrieved_at=retrieved_at if retrieved_at is not None else datetime.now(UTC),
        )

    def excerpt(self, limit: int = 400) -> str:
        """The opening of the document text, for inclusion in a prompt or a report."""
        stripped = " ".join(self.text.split())
        if len(stripped) <= limit:
            return stripped
        return stripped[:limit].rstrip() + "..."
