import hashlib
import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from osint_harness.domain.provenance import Document


class CassetteMissError(Exception):
    """Raised when a replay-only run needs an external call that was never recorded."""


class CassetteMode(StrEnum):
    """Whether a run may reach the network, or must be satisfied entirely from the recording."""

    REPLAY = "replay"
    RECORD = "record"


class RecordedCall(BaseModel):
    """One external lookup and the documents it returned, preserved verbatim for replay."""

    model_config = ConfigDict(frozen=True)

    tool: str = Field(min_length=1)
    query: str
    documents: tuple[Document, ...] = ()


class Cassette(BaseModel):
    """Every external call an investigation made, so a benchmark run can be repeated exactly.

    This is what makes the memory ablation honest: all three memory modes replay byte-identical
    retrieval, so a measured difference is attributable to memory rather than to the live web
    changing between runs.
    """

    calls: dict[str, RecordedCall] = Field(default_factory=dict)
    mode: CassetteMode = Field(default=CassetteMode.REPLAY, exclude=True)

    @classmethod
    def key_for(cls, tool: str, query: str) -> str:
        """A stable identifier for one lookup, insensitive to incidental whitespace."""
        normalised = " ".join(query.split())
        return hashlib.sha256(f"{tool}\n{normalised}".encode()).hexdigest()[:16]

    @classmethod
    def load(cls, path: Path, mode: CassetteMode = CassetteMode.REPLAY) -> "Cassette":
        """Read a cassette from disk, or start an empty one if the file does not exist yet."""
        if not path.exists():
            return cls(mode=mode)
        cassette = cls.model_validate_json(path.read_text(encoding="utf-8"))
        cassette.mode = mode
        return cassette

    def save(self, path: Path) -> None:
        """Write the cassette to disk, creating the directory if needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(self.model_dump_json())
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def holds(self, key: str) -> bool:
        """Whether this lookup has already been recorded."""
        return key in self.calls

    def replay(self, key: str) -> tuple[Document, ...]:
        """The documents a recorded lookup returned."""
        if key not in self.calls:
            raise CassetteMissError(f"no recording for {key}")
        return self.calls[key].documents

    def record(self, key: str, call: RecordedCall) -> None:
        """Store a live lookup so later runs can replay it."""
        self.calls[key] = call
