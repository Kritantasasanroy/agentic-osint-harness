import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar
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

    _HOSTNAME: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:https?://|//)?"
        r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)",
        re.IGNORECASE,
    )

    domain: str = Field(min_length=1)
    reliability: SourceReliability = SourceReliability.CANNOT_BE_JUDGED
    reason: str = "not yet assessed"

    @classmethod
    def registrable_domain(cls, url: str) -> str:
        """The host portion of a URL, lowercased and stripped of a leading www."""
        host = urlparse(url).netloc.lower()
        return host.partition(":")[0].removeprefix("www.")

    @classmethod
    def domain_only(cls, text: str) -> str:
        """A bare registrable domain, wherever it appears in free text and however it is dressed.

        Two earlier versions of this each fixed the exact shape an audit had just broken and
        nothing more: first a plain first-token split (broken by trailing commentary), then a
        first-token split with punctuation-stripping and URL-detection (broken by a domain with
        no scheme sitting mid-sentence, e.g. `"the domain is en.wikipedia.org"`, and a bare
        `"domain.tld/path"` with no scheme to detect). Both were guessing the domain's *position*
        in the string. This searches for the *shape* of a hostname instead — labels separated by
        dots, with or without a leading scheme — wherever it occurs, which is what actually varies
        across how a model phrases "here is a domain." If nothing hostname-shaped is found at all,
        the text is returned as-is (lowercased): a model returning pure garbage for this field is
        a different failure than mis-formatting a real answer, and no string transform fixes it.
        """
        match = cls._HOSTNAME.search(text)
        if match is None:
            return text.strip().lower()
        return match.group(1).lower().removeprefix("www.")

    def regrade(self, reliability: SourceReliability, reason: str) -> None:
        """Record a new reliability grade together with the justification for it."""
        self.reliability = reliability
        self.reason = reason


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
