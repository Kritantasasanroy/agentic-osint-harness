import json
from pathlib import Path
from typing import Self

from pydantic import BaseModel


class Persisted(BaseModel):
    """A record kept as JSON between runs.

    Three things outlive a single episode — the cassette, the investigation archive and the source
    register — and each needs the same three behaviours: start empty when the file does not exist
    yet, round-trip through the model's own schema, and write stable output. Writing that three
    times invited them to drift apart, so it is written once here.

    Output is sorted and indented deliberately: these files are committed and reviewed, and a diff
    that reorders itself on every save is a diff nobody reads.
    """

    @classmethod
    def read_from(cls, path: Path) -> Self:
        """Read the record, or start an empty one where no file exists yet."""
        if not path.exists():
            return cls()
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def write_to(self, path: Path) -> None:
        """Write the record as stable, diffable JSON, creating the directory if needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(self.model_dump_json())
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
