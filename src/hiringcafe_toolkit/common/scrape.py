"""Fetch raw job records for one or more searches. Shared by every pipeline.

Writes two files per run:

``jobs-<timestamp>.jsonl[.gz]``
    One raw record per line, deduplicated by ``objectID`` across pages and
    across searches, and otherwise untouched. Keeping the records verbatim
    means later stages can be rewritten and re-run without re-scraping.

``meta-<timestamp>.json``
    Audit trail, one entry per search: the searchState used, per-page
    received/new counts, build ids, any totals the API reported, and the
    reason iteration stopped. This is what tells you whether a run actually
    exhausted each result set. Left uncompressed - it is small and read by eye
    after every run.

Several searches share one output file rather than one file each, because the
later stages pick up "the newest raw file": two files per run would leave one
of them unprocessed. Searches overlap (a remote job near home matches both a
local and a nationwide search), and writing each record once keeps the overlap
from reaching normalization as a duplicate.

This module takes no view on what the records mean. Both pipelines scrape
identically and diverge only afterwards, so this lives in ``common`` rather
than being forked per pipeline.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from hiringcafe_toolkit.api import HiringCafeError, ResultPage, record_key
from hiringcafe_toolkit.common.jsonl import JsonlWriter, unique_path, with_compression

JsonDict = dict[str, Any]

#: Pagination re-serves some records, so a single page of duplicates does not
#: mean the result set is exhausted. Two in a row does.
ZERO_NEW_PAGES_BEFORE_STOP = 2

#: Stop reason when a page source runs out before the page ceiling.
STOP_SOURCE_ENDED = "no more pages"

logger = logging.getLogger(__name__)


class PageSource(Protocol):
    """What this stage needs from a client: pages of records.

    Depending on the protocol rather than the concrete client keeps the stage
    testable without HTTP and leaves room for a replayed-from-disk source.
    """

    def iter_pages(
        self, search_state: Mapping[str, Any], max_pages: int
    ) -> Iterator[ResultPage]: ...


@dataclass(frozen=True)
class Search:
    """One searchState to scrape, named for the meta sidecar."""

    name: str
    state: Mapping[str, Any]


@dataclass(frozen=True)
class SearchResult:
    """Outcome of one search within a run."""

    name: str
    unique_records: int
    """Distinct records this search returned, including ones an earlier
    search in the same run had already written."""
    already_written: int
    """Of those, how many an earlier search found first. The overlap between
    searches, and the reason the file total is less than the sum."""
    pages_fetched: int
    stop_reason: str
    reported_totals: dict[str, float] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        return self.stop_reason.startswith("reached max_pages")


@dataclass(frozen=True)
class ScrapeResult:
    """Outcome of one scrape run."""

    jobs_path: Path
    meta_path: Path
    unique_records: int
    pages_fetched: int
    stop_reason: str
    searches: tuple[SearchResult, ...] = ()

    @property
    def truncated(self) -> bool:
        """Whether any search stopped at the page ceiling rather than the end."""
        return any(search.truncated for search in self.searches)


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H%M%S")


@dataclass
class _Progress:
    """Running state for one search, readable at any point for the meta."""

    search: Search
    keys: set[str] = field(default_factory=set)
    """Keys this search has returned. Kept apart from the run-wide written set:
    the stop rule must count records new to *this* search, because a
    nationwide search run after a local one opens with pages of jobs the local
    one already wrote, and counting those as stale would end it after two."""
    already_written: int = 0
    keyless: int = 0
    pages: list[JsonDict] = field(default_factory=list)
    build_ids: list[str] = field(default_factory=list)
    reported_totals: dict[str, float] = field(default_factory=dict)
    stop_reason: str = "unknown"

    def as_meta(self) -> JsonDict:
        return {
            "variant": self.search.name,
            "searchState": dict(self.search.state),
            "pages": self.pages,
            "build_ids": self.build_ids,
            "reported_totals": self.reported_totals,
            "unique_records": len(self.keys),
            "already_written": self.already_written,
            "keyless_records": self.keyless,
            "stop_reason": self.stop_reason,
        }

    def result(self) -> SearchResult:
        return SearchResult(
            name=self.search.name,
            unique_records=len(self.keys),
            already_written=self.already_written,
            pages_fetched=len(self.pages),
            stop_reason=self.stop_reason,
            reported_totals=dict(self.reported_totals),
        )


def _overall_stop_reason(progress: Sequence[_Progress]) -> str:
    """One search's reason as is; several, each labelled by name."""
    if not progress:
        return "unknown"
    if len(progress) == 1:
        return progress[0].stop_reason
    return "; ".join(f"{p.search.name}: {p.stop_reason}" for p in progress)


def _scrape_one(
    progress: _Progress,
    writer: JsonlWriter,
    written_keys: set[str],
    client: PageSource,
    max_pages: int,
) -> None:
    """Page through one search, writing records no earlier search wrote."""
    name = progress.search.name
    consecutive_zero_new = 0
    for page in client.iter_pages(progress.search.state, max_pages):
        if page.build_id and page.build_id not in progress.build_ids:
            progress.build_ids.append(page.build_id)
        progress.reported_totals.update(page.reported_totals)

        new_count = 0
        for record in page.records:
            key = record_key(record)
            if key is None:
                # No usable id: keep it (raw data is preserved as-is) but
                # count it, since it cannot be deduplicated.
                progress.keyless += 1
                writer.write(record)
                new_count += 1
                continue
            if key in progress.keys:
                continue
            progress.keys.add(key)
            new_count += 1
            if key in written_keys:
                progress.already_written += 1
                continue
            written_keys.add(key)
            writer.write(record)
        writer.flush()

        progress.pages.append({"page": page.label, "received": len(page.records), "new": new_count})
        logger.info(
            "%s page %s: %d records, %d new (unique so far: %d)",
            name,
            page.label,
            len(page.records),
            new_count,
            len(progress.keys),
        )

        if not page.records:
            progress.stop_reason = "empty page"
            return

        consecutive_zero_new = consecutive_zero_new + 1 if new_count == 0 else 0
        if consecutive_zero_new >= ZERO_NEW_PAGES_BEFORE_STOP:
            progress.stop_reason = f"{consecutive_zero_new} consecutive pages with no new records"
            return
    # The live client yields the first page plus up to ``max_pages`` more, so
    # running out short of that means the source itself ended - a capture
    # file whose last page was not empty, say - rather than the ceiling.
    if len(progress.pages) > max_pages:
        progress.stop_reason = f"reached max_pages={max_pages}"
    else:
        progress.stop_reason = STOP_SOURCE_ENDED


def _drop_if_empty(jobs_path: Path, written_keys: set[str], progress: Sequence[_Progress]) -> None:
    """Remove a failed run's records file when nothing was written to it.

    Later stages default to the newest raw file, so an empty one left behind by
    a run that failed on its first request would be picked up as a day with no
    postings. The meta sidecar stays as the record of the failure.
    """
    if not written_keys and not any(p.keyless for p in progress):
        jobs_path.unlink(missing_ok=True)


def run_scrape(
    searches: Sequence[Search],
    out_dir: Path,
    *,
    client: PageSource,
    max_pages: int,
    compress: bool = True,
    extra_meta: Mapping[str, Any] | None = None,
) -> ScrapeResult:
    """Scrape each search in turn into one raw file plus a meta sidecar.

    ``max_pages`` applies to each search separately. ``extra_meta`` is merged
    into the sidecar, for facts only the caller knows - where a capture file
    came from and which of its pages were missing.
    """
    if not searches:
        raise ValueError("run_scrape needs at least one search")

    stamp = _timestamp()
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = unique_path(with_compression(out_dir / f"jobs-{stamp}.jsonl", compress))
    meta_path = unique_path(out_dir / f"meta-{stamp}.json")

    written_keys: set[str] = set()
    progress: list[_Progress] = []
    error: str | None = None

    meta: JsonDict = {
        "started_at": datetime.now(UTC).isoformat(),
        "max_pages": max_pages,
        "jobs_file": jobs_path.name,
        "compressed": compress,
        **(extra_meta or {}),
    }

    def write_meta() -> None:
        meta["finished_at"] = datetime.now(UTC).isoformat()
        meta["unique_records"] = len(written_keys)
        meta["keyless_records"] = sum(p.keyless for p in progress)
        meta["pages_fetched"] = sum(len(p.pages) for p in progress)
        meta["stop_reason"] = _overall_stop_reason(progress)
        if error is not None:
            meta["error"] = error
        if not jobs_path.exists():
            meta["jobs_file"] = None  # dropped as empty after a failure
        meta["searches"] = [p.as_meta() for p in progress]
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    try:
        with JsonlWriter(jobs_path) as writer:
            for search in searches:
                progress.append(_Progress(search))
                _scrape_one(progress[-1], writer, written_keys, client, max_pages)
    except HiringCafeError as exc:
        progress[-1].stop_reason = "error"
        error = f"{progress[-1].search.name}: {exc}" if len(searches) > 1 else str(exc)
        _drop_if_empty(jobs_path, written_keys, progress)
        write_meta()
        raise
    except KeyboardInterrupt:
        if progress:
            progress[-1].stop_reason = "interrupted"
        _drop_if_empty(jobs_path, written_keys, progress)
        write_meta()
        raise

    write_meta()
    return ScrapeResult(
        jobs_path=jobs_path,
        meta_path=meta_path,
        unique_records=len(written_keys),
        pages_fetched=sum(len(p.pages) for p in progress),
        stop_reason=_overall_stop_reason(progress),
        searches=tuple(p.result() for p in progress),
    )
