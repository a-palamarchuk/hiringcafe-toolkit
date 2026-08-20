"""JSON Lines helpers.

Pipeline stages exchange one JSON object per line: greppable, viewable with
``less``, line-diffable between runs, and parseable one record at a time
without holding a whole run in memory.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

JsonDict = dict[str, Any]


def read_jsonl(path: Path) -> Iterator[JsonDict]:
    """Yield records from a JSONL file, skipping blank lines."""
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record: Any = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            yield record


class JsonlWriter:
    """Appends records to a JSONL file, flushing as it goes.

    Flushing per batch means an interrupted run keeps everything fetched so
    far, which matters when a run takes minutes and the upstream API is
    undocumented.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handle = path.open("w", encoding="utf-8")

    def __enter__(self) -> JsonlWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def write(self, record: JsonDict) -> None:
        self._handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")

    def flush(self) -> None:
        self._handle.flush()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
