#!/usr/bin/env python3
"""Diagnose whether a scrape run actually retrieved its result set.

A run that stops on ``empty page`` looks clean but can still be far short of
the total the API reported. This asks how short, and whether the shortfall is
duplicate collapsing, pagination giving out, or a filter applied after the
count was taken.

The company-coverage check is the load-bearing one. Postings and companies are
counted separately by the API, so comparing both against what landed on disk
separates "pagination dropped whole companies" (bad, split the search) from
"pagination dropped extra postings at companies already retrieved" (tolerable,
since the company still surfaces).

Usage:
    python3 scripts/inspect_scrape.py data/job_shortlist/raw/jobs-*.jsonl.gz
    python3 scripts/inspect_scrape.py <jobs file> --meta <meta file>
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

JsonDict = dict[str, Any]

BAR_WIDTH = 40


def read_records(path: Path) -> Iterator[JsonDict]:
    """Yield records from a JSONL file, gzipped or plain."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{number}: invalid JSON: {exc}") from exc
            yield record


def normalize_host(value: Any) -> str | None:
    """Reduce a URL or bare hostname to a lowercase host without ``www.``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "//" not in text:
        text = "//" + text
    host = urlsplit(text).hostname
    if not host:
        return None
    host = host.strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host else None


def sub(record: JsonDict, key: str) -> JsonDict:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def pct(part: int, whole: float) -> str:
    return f"{part / whole * 100:5.1f}%" if whole else "    - "


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs", type=Path, help="jobs-*.jsonl or .jsonl.gz")
    parser.add_argument("--meta", type=Path, help="meta-*.json (default: guessed alongside)")
    args = parser.parse_args()

    records = list(read_records(args.jobs))
    if not records:
        raise SystemExit(f"{args.jobs}: no records")

    meta_path = args.meta
    if meta_path is None:
        # meta-<stamp>.json sits beside jobs-<stamp>.jsonl[.gz]; derive the
        # stamp rather than making the caller pass both paths.
        stem = args.jobs.name.removesuffix(".gz").removesuffix(".jsonl")
        guess = args.jobs.parent / f"meta-{stem.removeprefix('jobs-')}.json"
        meta_path = guess if guess.exists() else None
    meta: JsonDict = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path else {}
    totals = meta.get("reported_totals") or {}
    reported_jobs = totals.get("ssrTotalCount")
    reported_companies = totals.get("ssrCompanyCount")

    ids = [r.get("objectID") for r in records if r.get("objectID")]
    hosts = {
        h
        for r in records
        if (h := normalize_host(sub(r, "enriched_company_data").get("homepage_uri")))
    }
    names = {
        n
        for r in records
        if isinstance(n := sub(r, "enriched_company_data").get("name"), str) and n.strip()
    }

    print(f"file: {args.jobs}")
    if meta_path:
        print(f"meta: {meta_path}  (stop_reason: {meta.get('stop_reason', '?')})")

    section("1. Coverage vs what the API claimed")
    print(f"  records on disk        {len(records):6d}")
    print(f"  distinct objectID      {len(set(ids)):6d}")
    if reported_jobs:
        print(
            f"  ssrTotalCount          {int(reported_jobs):6d}   retrieved "
            f"{pct(len(set(ids)), reported_jobs)}"
        )
    print(f"  distinct company host  {len(hosts):6d}")
    print(f"  distinct company name  {len(names):6d}")
    if reported_companies:
        print(
            f"  ssrCompanyCount        {int(reported_companies):6d}   retrieved "
            f"{pct(len(names), reported_companies)}  <-- the discriminating number"
        )

    section("2. Duplicate structure (could collapsing explain the gap?)")
    for key in ("collapse_key", "liberal_dedup_cluster", "strict_dedup_cluster_id"):
        present = [r[key] for r in records if r.get(key)]
        distinct = len(set(present))
        groups = sum(1 for n in Counter(present).values() if n > 1)
        print(
            f"  {key:24s} present {len(present):6d}  distinct {distinct:6d}  "
            f"multi-member groups {groups:5d}"
        )
    # Collapsing on the true duplicate key, with a fallback for records that
    # carry none, is the honest count of distinct jobs retrieved.
    collapsed = {
        r.get("liberal_dedup_cluster")
        or (
            sub(r, "v5_processed_job_data").get("company_name"),
            sub(r, "v5_processed_job_data").get("core_job_title"),
        )
        for r in records
    }
    print(f"\n  distinct jobs after collapsing: {len(collapsed)}")
    if reported_jobs:
        print(
            f"  If the API counted pre-collapse, it would need ~"
            f"{reported_jobs / max(len(collapsed), 1):.1f}x duplication to reach "
            f"{int(reported_jobs)}."
        )

    section("3. Publish-date span (did pagination truncate one end?)")
    dates = sorted(
        d[:10]
        for r in records
        if isinstance(d := sub(r, "v5_processed_job_data").get("estimated_publish_date"), str)
    )
    if dates:
        print(f"  range {dates[0]} .. {dates[-1]}   ({len(set(dates))} distinct days)")
        # Daily buckets: the fetch window is weeks, so a monthly histogram
        # would collapse the whole run into one bar and hide the cliff.
        buckets = Counter(dates)
        peak = max(buckets.values())
        for day, count in sorted(buckets.items()):
            bar = "#" * max(1, round(count / peak * BAR_WIDTH))
            print(f"    {day}  {count:5d}  {bar}")
        span = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days + 1
        window = (meta.get("searchState") or {}).get("dateFetchedPastNDays")
        print(f"\n  publish dates span {span} days", end="")
        if isinstance(window, int) and window > 0:
            print(f"; fetch window was {window} days")
            if span < window * 0.6:
                print("  The publish range is far shorter than the fetch window. Postings")
                print("  are fetched well after publication, so this alone is not proof of")
                print("  truncation - but combined with low company coverage it points to it.")
        else:
            print()
        print("  An abrupt cliff at the old end means pagination stopped before the")
        print("  window did. A smooth taper is normal: fewer old postings survive.")

    section("4. Post-count filtering (was the total taken before these dropped?)")
    expired = sum(1 for r in records if r.get("is_expired") is True)
    print(f"  is_expired = true      {expired:6d}  ({pct(expired, len(records))} of file)")

    section("5. Where the records came from")
    cats = Counter(sub(r, "v5_processed_job_data").get("job_category") or "(none)" for r in records)
    for name, count in cats.most_common():
        print(f"  {name:38s} {count:6d}  {pct(count, len(records))}")
    print()
    srcs = Counter(r.get("source") or "(none)" for r in records)
    print(
        f"  distinct ATS sources   {len(srcs):6d}   top: "
        + ", ".join(f"{k}({v})" for k, v in srcs.most_common(6))
    )

    section("Verdict")
    if reported_companies and names:
        ratio = len(names) / reported_companies
        if ratio >= 0.9:
            print("  Company coverage is essentially complete. Pagination is dropping extra")
            print("  postings at companies you already have, not whole companies. Every")
            print("  employer still reaches your shortlist. Tolerable; no split needed.")
        elif ratio >= 0.6:
            print("  Partial company coverage. Some employers never appear at all. Worth")
            print("  splitting the search and unioning the results.")
        else:
            print("  Company coverage tracks the posting shortfall, so pagination is")
            print("  genuinely truncating. Split the search: run one searchState per")
            print("  department, or narrow the date window, and union the outputs.")
    else:
        print("  No ssrCompanyCount in meta; rerun with --meta to get the verdict.")
    print("\n  Definitive test either way: scrape each department separately and count")
    print("  unique objectIDs across the union. If that exceeds this run's total,")
    print("  a single search cannot reach the whole result set.")


if __name__ == "__main__":
    main()
