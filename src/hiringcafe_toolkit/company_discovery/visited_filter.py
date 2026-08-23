"""Stage 3 of company discovery: drop companies already visited.

The VisitLogger browser extension exports a JSON object keyed by website host::

    {"1password.com": {"date": "2026-07-10", "r": true},
     "6sense.com": {"date": "2026-07-10"}}

Both sides are reduced through the same host normalization, so a company row is
excluded when its website host appears in the export.

Only rows with a company website can be filtered. Rows without one are keyed on
an ATS URL, and marking one visited would be recorded against a host shared by
thousands of unrelated employers, so those pass through untouched and are
reviewed separately.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.common.jsonl import JsonlWriter
from hiringcafe_toolkit.common.urls import POSTING_KEY_PREFIX, normalize_host

JsonDict = dict[str, Any]

logger = logging.getLogger(__name__)


class VisitLogError(Exception):
    """The visit log is missing or not in the expected shape."""


@dataclass(frozen=True)
class VisitLog:
    """Normalized view of a VisitLogger export."""

    hosts: frozenset[str]
    applied_hosts: frozenset[str]
    """Hosts flagged with ``r`` - a resume was submitted there."""

    entries_read: int
    unusable_keys: tuple[str, ...]
    posting_keys: int = 0
    """Keys belonging to the job-shortlist pipeline, skipped rather than counted
    as unusable. The export is one flat object, so both pipelines share it."""


def load_visit_log(path: Path) -> VisitLog:
    """Read and normalize a VisitLogger export."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise VisitLogError(f"{path}: visit log not found") from exc
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VisitLogError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise VisitLogError(f"{path}: expected a JSON object keyed by host")

    hosts: set[str] = set()
    applied: set[str] = set()
    unusable: list[str] = []
    posting_keys = 0
    for key, value in parsed.items():
        if isinstance(key, str) and key.startswith(POSTING_KEY_PREFIX):
            # The export is one flat object, so the job-shortlist pipeline's
            # posting keys share this file. They are a recognized category, not
            # a normalization failure - counting them as unusable would bury
            # the diagnostic that field exists for under thousands of entries.
            posting_keys += 1
            continue
        host = normalize_host(key)
        if host is None:
            unusable.append(str(key))
            continue
        hosts.add(host)
        if isinstance(value, Mapping) and value.get("r") is True:
            applied.add(host)

    return VisitLog(
        hosts=frozenset(hosts),
        applied_hosts=frozenset(applied),
        entries_read=len(parsed),
        unusable_keys=tuple(unusable),
        posting_keys=posting_keys,
    )


@dataclass(frozen=True)
class FilterResult:
    remaining_path: Path
    meta_path: Path
    kept: int
    excluded: int
    passthrough: int


def filter_companies(
    companies: Iterable[Mapping[str, Any]], visit_log: VisitLog
) -> tuple[list[JsonDict], list[JsonDict], JsonDict]:
    """Split company rows into kept and already-visited.

    Returns ``(kept, excluded, stats)``.
    """
    kept: list[JsonDict] = []
    excluded: list[JsonDict] = []
    stats: Counter[str] = Counter()

    for company in companies:
        stats["companies_read"] += 1
        host = company.get("website_host")
        host = normalize_host(host) if isinstance(host, str) else None

        if host is None:
            # No company domain to match on; cannot be deduplicated.
            stats["kept_unmatchable"] += 1
            kept.append(dict(company))
            continue

        if host in visit_log.hosts:
            stats["excluded_visited"] += 1
            if host in visit_log.applied_hosts:
                stats["excluded_already_applied"] += 1
            excluded.append(dict(company))
            continue

        kept.append(dict(company))

    stats["kept"] = len(kept)
    return kept, excluded, dict(stats)


def run_filter(
    companies: Iterable[Mapping[str, Any]],
    visit_log: VisitLog,
    out_dir: Path,
    *,
    tail: Iterable[Mapping[str, Any]] | None = None,
    source_path: Path | None = None,
    visit_log_path: Path | None = None,
) -> FilterResult:
    """Write the companies still worth visiting, plus a meta sidecar."""
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    remaining_path = out_dir / f"remaining-{stamp}.jsonl"
    tail_path = out_dir / f"remaining-no-website-{stamp}.jsonl"
    meta_path = out_dir / f"filter-meta-{stamp}.json"

    kept, excluded, stats = filter_companies(companies, visit_log)

    with JsonlWriter(remaining_path) as writer:
        for company in kept:
            writer.write(company)

    passthrough = 0
    with JsonlWriter(tail_path) as tail_writer:
        for tail_company in tail or ():
            tail_writer.write(dict(tail_company))
            passthrough += 1

    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": str(source_path) if source_path else None,
        "visit_log": str(visit_log_path) if visit_log_path else None,
        "visit_log_entries": visit_log.entries_read,
        "visit_log_hosts": len(visit_log.hosts),
        "visit_log_unusable_keys": list(visit_log.unusable_keys),
        "stats": {**stats, "passthrough_no_website": passthrough},
        # A sample of what was excluded, so a normalization bug shows up as an
        # obviously wrong match rather than a silently smaller output file.
        "excluded_sample": sorted(company.get("website_host", "") for company in excluded)[:25],
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    return FilterResult(
        remaining_path=remaining_path,
        meta_path=meta_path,
        kept=len(kept),
        excluded=len(excluded),
        passthrough=passthrough,
    )
