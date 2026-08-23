"""Fetch raw job records for a search. Shared by every pipeline.

Writes two files per run:

``jobs-<timestamp>.jsonl[.gz]``
    One raw record per line, deduplicated by ``objectID`` across pages and
    otherwise untouched. Keeping the records verbatim means later stages can be
    rewritten and re-run without re-scraping.

``meta-<timestamp>.json``
    Audit trail: the searchState used, per-page received/new counts, build ids,
    any totals the API reported, and the reason iteration stopped. This is what
    tells you whether a run actually exhausted the result set. Left
    uncompressed - it is small and read by eye after every run.

This module takes no view on what the records mean. Both pipelines scrape
identically and diverge only afterwards, so this lives in ``common`` rather
than being forked per pipeline.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from hiringcafe_toolkit.api import HiringCafeError, ResultPage, record_key
from hiringcafe_toolkit.common.jsonl import JsonlWriter, unique_path, with_compression

JsonDict = dict[str, Any]

#: Pagination re-serves some records, so a single page of duplicates does not
#: mean the result set is exhausted. Two in a row does.
ZERO_NEW_PAGES_BEFORE_STOP = 2

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
class ScrapeResult:
    """Outcome of one scrape run."""

    jobs_path: Path
    meta_path: Path
    unique_records: int
    pages_fetched: int
    stop_reason: str
    reported_totals: dict[str, float]


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H%M%S")


def run_scrape(
    search_state: Mapping[str, Any],
    out_dir: Path,
    *,
    client: PageSource,
    max_pages: int,
    variant_name: str = "default",
    compress: bool = True,
) -> ScrapeResult:
    """Scrape one search and write raw records plus a meta sidecar."""
    stamp = _timestamp()
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = unique_path(with_compression(out_dir / f"jobs-{stamp}.jsonl", compress))
    meta_path = unique_path(out_dir / f"meta-{stamp}.json")

    seen_keys: set[str] = set()
    keyless_records = 0
    pages_meta: list[JsonDict] = []
    build_ids: list[str] = []
    reported_totals: dict[str, float] = {}
    consecutive_zero_new = 0
    stop_reason = "unknown"
    error: str | None = None

    meta: JsonDict = {
        "started_at": datetime.now(UTC).isoformat(),
        "variant": variant_name,
        "searchState": dict(search_state),
        "max_pages": max_pages,
        "jobs_file": jobs_path.name,
        "compressed": compress,
        "pages": pages_meta,
        "build_ids": build_ids,
        "reported_totals": reported_totals,
    }

    def write_meta() -> None:
        meta["finished_at"] = datetime.now(UTC).isoformat()
        meta["unique_records"] = len(seen_keys)
        meta["keyless_records"] = keyless_records
        meta["pages_fetched"] = len(pages_meta)
        meta["stop_reason"] = stop_reason
        if error is not None:
            meta["error"] = error
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    try:
        with JsonlWriter(jobs_path) as writer:
            for page in client.iter_pages(search_state, max_pages):
                if page.build_id and page.build_id not in build_ids:
                    build_ids.append(page.build_id)
                reported_totals.update(page.reported_totals)

                new_count = 0
                for record in page.records:
                    key = record_key(record)
                    if key is None:
                        # No usable id: keep it (raw data is preserved as-is)
                        # but count it, since it cannot be deduplicated.
                        keyless_records += 1
                    elif key in seen_keys:
                        continue
                    else:
                        seen_keys.add(key)
                    writer.write(record)
                    new_count += 1
                writer.flush()

                pages_meta.append(
                    {"page": page.label, "received": len(page.records), "new": new_count}
                )
                logger.info(
                    "page %s: %d records, %d new (unique so far: %d)",
                    page.label,
                    len(page.records),
                    new_count,
                    len(seen_keys),
                )

                if not page.records:
                    stop_reason = "empty page"
                    break

                consecutive_zero_new = consecutive_zero_new + 1 if new_count == 0 else 0
                if consecutive_zero_new >= ZERO_NEW_PAGES_BEFORE_STOP:
                    stop_reason = f"{consecutive_zero_new} consecutive pages with no new records"
                    break
            else:
                stop_reason = f"reached max_pages={max_pages}"
    except HiringCafeError as exc:
        stop_reason = "error"
        error = str(exc)
        write_meta()
        raise
    except KeyboardInterrupt:
        stop_reason = "interrupted"
        write_meta()
        raise

    write_meta()
    return ScrapeResult(
        jobs_path=jobs_path,
        meta_path=meta_path,
        unique_records=len(seen_keys),
        pages_fetched=len(pages_meta),
        stop_reason=stop_reason,
        reported_totals=dict(reported_totals),
    )
