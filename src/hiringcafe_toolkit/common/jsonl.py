"""JSON Lines helpers.

Pipeline stages exchange one JSON object per line: greppable, viewable with
``less``, line-diffable between runs, and parseable one record at a time
without holding a whole run in memory.

Files may be plain or gzipped, decided by a ``.gz`` suffix on the path. Raw
scrape output compresses about 7x - these records are mostly repeated JSON
keys - which matters once a daily run is retained indefinitely. Reading accepts
both forms unconditionally so files written before compression existed keep
working without a migration.
"""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import IO, Any

JsonDict = dict[str, Any]

GZIP_SUFFIX = ".gz"


def is_compressed(path: Path) -> bool:
    """True if the path names a gzipped file."""
    return path.suffix == GZIP_SUFFIX


def with_compression(path: Path, compress: bool) -> Path:
    """Add or remove the ``.gz`` suffix so the name matches the encoding.

    Callers build a base name (``jobs-<stamp>.jsonl``) and let this settle the
    encoding, rather than each one string-concatenating a suffix and eventually
    producing ``jobs.jsonl.gz.gz``.
    """
    if compress:
        return path if is_compressed(path) else path.with_suffix(path.suffix + GZIP_SUFFIX)
    return path.with_suffix("") if is_compressed(path) else path


def _open_text(path: Path, mode: str) -> IO[str]:
    """Open a path as text, transparently gzipping when the name says ``.gz``.

    ``GzipFile`` is constructed directly rather than via ``gzip.open`` so that
    ``mtime=0`` can be set: otherwise the current time is written into the gzip
    header, and two runs over identical records produce different bytes.
    Flushing a ``GzipFile`` emits a zlib sync point, so an interrupted run
    leaves a file that still decompresses up to the last flush.
    """
    if is_compressed(path):
        raw = gzip.GzipFile(filename=path, mode=mode + "b", compresslevel=6, mtime=0)
        return io.TextIOWrapper(raw, encoding="utf-8")
    return path.open(mode, encoding="utf-8")


def read_jsonl(path: Path) -> Iterator[JsonDict]:
    """Yield records from a JSONL file, plain or gzipped, skipping blank lines."""
    with _open_text(path, "r") as handle:
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
    undocumented. Gzip flushes are full sync points, so a truncated file stays
    readable up to the last flush rather than failing to decompress.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handle = _open_text(path, "w")

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


def unique_path(path: Path) -> Path:
    """A path that does not already exist, suffixing ``-2``, ``-3``, ... if needed.

    Stage outputs are stamped to the second, so two runs inside one second
    would otherwise write the same name and the later silently replace the
    earlier. That is cheap to prevent and expensive to notice.
    """
    if not path.exists():
        return path
    stem = path.name
    suffixes = ""
    while "." in stem:
        stem, dot, tail = stem.rpartition(".")
        suffixes = f"{dot}{tail}{suffixes}"
    for attempt in range(2, 1000):
        candidate = path.with_name(f"{stem}-{attempt}{suffixes}")
        if not candidate.exists():
            return candidate
    raise OSError(f"{path}: could not find an unused name")
