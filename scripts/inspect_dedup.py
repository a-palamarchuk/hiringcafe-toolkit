#!/usr/bin/env python3
"""Decide what the duplicate-detection fallback key should be.

``liberal_dedup_cluster`` is hiring.cafe's own cross-ATS duplicate key, but it
is absent on most records, so a fallback is what actually does the work. A
fallback that is too loose merges two distinct roles at one company into one
row and the second is never seen; too tight and the same job is reviewed
repeatedly.

The method here is to calibrate against ground truth rather than guess.
Records that *do* carry a cluster key are known duplicates, so the similarity
of their ``requirements_summary`` text establishes what a real duplicate looks
like. Candidate fallback keys are then scored against that baseline: groups
whose text similarity matches the baseline are believable merges, and groups
far below it are over-collapsing.

Usage:
    python3 scripts/inspect_dedup.py data/job_shortlist/raw/jobs-*.jsonl.gz
    python3 scripts/inspect_dedup.py <jobs file> --examples 8
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from itertools import combinations
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]

#: Used only when the file has too few cluster groups to calibrate against.
#: The real threshold is derived from ground truth at run time: a guessed
#: constant here would flag the API's own merges as errors, which is how the
#: first version of this script got it wrong.
FALLBACK_SIMILARITY = 0.5

#: Percentile of ground-truth similarity below which a merge is doubted. Real
#: duplicates vary a lot - the same job rewritten for a second ATS shares only
#: some of its text - so the bar is the weak end of known-good merges, not
#: their average.
GROUND_TRUTH_PERCENTILE = 10

#: Cap on pairwise comparisons inside one group. Groups are tiny in practice;
#: this only stops a pathological group from dominating the runtime.
MAX_PAIRS_PER_GROUP = 15

WORD = re.compile(r"[a-z0-9+#.]+")


def read_records(path: Path) -> Iterator[JsonDict]:
    """Yield records from a JSONL file, gzipped or plain."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{number}: invalid JSON: {exc}") from exc


def sub(record: JsonDict, key: str) -> JsonDict:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def tokens(value: str) -> frozenset[str]:
    return frozenset(WORD.findall(value.lower()))


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Token overlap. Cheap, order-insensitive, and adequate for near-identical text."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def group_similarity(members: Sequence[JsonDict]) -> float | None:
    """Mean pairwise similarity of a group's requirements text.

    Returns None when too few members carry text to compare, so "no evidence"
    stays distinct from "evidence of dissimilarity".
    """
    texts = [
        tokens(text(sub(record, "v5_processed_job_data").get("requirements_summary")))
        for record in members
    ]
    texts = [t for t in texts if t]
    if len(texts) < 2:
        return None
    pairs = list(combinations(texts, 2))[:MAX_PAIRS_PER_GROUP]
    return statistics.fmean(jaccard(a, b) for a, b in pairs)


def company(record: JsonDict) -> str:
    job = sub(record, "v5_processed_job_data")
    return (
        text(sub(record, "enriched_company_data").get("name"))
        or text(job.get("company_name"))
        or "(unknown)"
    )


def title(record: JsonDict) -> str:
    return text(sub(record, "v5_processed_job_data").get("core_job_title")) or text(
        sub(record, "job_information").get("title")
    )


def cities(record: JsonDict) -> tuple[str, ...]:
    value = sub(record, "v5_processed_job_data").get("workplace_cities")
    return tuple(sorted(text(c) for c in value)) if isinstance(value, list) else ()


def normalized_title(record: JsonDict) -> str:
    """Title with seniority and roman-numeral level markers stripped.

    "Software Engineer II" and "Senior Software Engineer" are different jobs,
    but "Software Engineer" and "Software Engineer  " are not. This only
    normalizes whitespace and case; level words are deliberately preserved.
    """
    return " ".join(title(record).lower().split())


def candidate_keys(record: JsonDict) -> dict[str, tuple[Any, ...]]:
    """The fallback keys under evaluation, from loosest to tightest."""
    job = sub(record, "v5_processed_job_data")
    return {
        "(company, title)": (company(record), normalized_title(record)),
        "(company, title, cities)": (company(record), normalized_title(record), cities(record)),
        "(company, title, cities, source)": (
            company(record),
            normalized_title(record),
            cities(record),
            text(record.get("source")),
        ),
        "(company, title, cities, publish-day)": (
            company(record),
            normalized_title(record),
            cities(record),
            text(job.get("estimated_publish_date"))[:10],
        ),
    }


def describe(members: Sequence[JsonDict]) -> str:
    """One-line summary of what varies inside a group."""
    parts = []
    for label, values in (
        ("sources", {text(m.get("source")) for m in members}),
        ("cities", {cities(m) for m in members}),
        ("reqs", {text(m.get("requisition_id")) for m in members}),
        (
            "days",
            {
                text(sub(m, "v5_processed_job_data").get("estimated_publish_date"))[:10]
                for m in members
            },
        ),
    ):
        parts.append(f"{label}={len(values)}")
    return "  ".join(parts)


def section(name: str) -> None:
    print(f"\n{name}\n{'-' * len(name)}")


def report_groups(
    label: str,
    groups: dict[Any, list[JsonDict]],
    baseline: float | None,
    threshold: float,
) -> None:
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    merged = sum(len(v) - 1 for v in multi.values())
    scored = [(k, v, s) for k, v in multi.items() if (s := group_similarity(v)) is not None]
    believable = sum(1 for _, _, s in scored if s >= threshold)
    suspicious = [(k, v, s) for k, v, s in scored if s < threshold]

    print(f"  {label:38s} groups>1 {len(multi):5d}   records merged away {merged:5d}")
    if scored:
        mean = statistics.fmean(s for _, _, s in scored)
        print(
            f"  {'':38s} mean similarity {mean:.2f}"
            + (f" (baseline {baseline:.2f})" if baseline is not None else "")
            + f"   believable {believable}/{len(scored)}"
            f"   suspicious {len(suspicious)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs", type=Path, help="jobs-*.jsonl or .jsonl.gz")
    parser.add_argument(
        "--examples", type=int, default=6, help="Suspicious groups to print in full."
    )
    args = parser.parse_args()

    records = list(read_records(args.jobs))
    if not records:
        raise SystemExit(f"{args.jobs}: no records")

    with_cluster = [r for r in records if r.get("liberal_dedup_cluster")]
    without_cluster = [r for r in records if not r.get("liberal_dedup_cluster")]

    print(f"file: {args.jobs}")
    print(
        f"records {len(records)}   with cluster key {len(with_cluster)} "
        f"({len(with_cluster) / len(records) * 100:.1f}%)   without {len(without_cluster)}"
    )

    section("1. Ground truth: what a real duplicate looks like")
    truth: dict[Any, list[JsonDict]] = defaultdict(list)
    for record in with_cluster:
        truth[record["liberal_dedup_cluster"]].append(record)
    truth_multi = {k: v for k, v in truth.items() if len(v) > 1}
    truth_scores = [s for v in truth_multi.values() if (s := group_similarity(v)) is not None]
    baseline: float | None = statistics.fmean(truth_scores) if truth_scores else None
    if len(truth_scores) >= 10:
        threshold = statistics.quantiles(truth_scores, n=100)[GROUND_TRUTH_PERCENTILE - 1]
    elif truth_scores:
        threshold = min(truth_scores)
    else:
        threshold = FALLBACK_SIMILARITY

    print(f"  cluster groups with >1 member: {len(truth_multi)}")
    if truth_scores:
        assert baseline is not None
        quantiles = statistics.quantiles(truth_scores, n=4) if len(truth_scores) > 3 else []
        print(
            f"  requirements-text similarity: mean {baseline:.2f}, "
            f"min {min(truth_scores):.2f}, max {max(truth_scores):.2f}"
        )
        if quantiles:
            print(f"  quartiles: {quantiles[0]:.2f} / {quantiles[1]:.2f} / {quantiles[2]:.2f}")
        print(
            f"\n  Calibrated threshold (p{GROUND_TRUTH_PERCENTILE} of ground truth): "
            f"{threshold:.2f}"
        )
        print("  Real duplicates score far below 1.0 because the same job gets rewritten")
        print("  for each ATS. Judging the fallback against a guessed constant would")
        print("  condemn merges the API itself makes.")
    else:
        print(f"  Too few multi-member cluster groups to calibrate; using {threshold:.2f}.")

    section("2. Do cluster-keyed and cluster-less records differ?")
    for label, subset in (("with cluster", with_cluster), ("without cluster", without_cluster)):
        if not subset:
            continue
        srcs = Counter(text(r.get("source")) for r in subset)
        cats = Counter(text(sub(r, "v5_processed_job_data").get("job_category")) for r in subset)
        print(
            f"  {label:16s} top sources: " + ", ".join(f"{k}({v})" for k, v in srcs.most_common(4))
        )
        print(
            f"  {'':16s} top categories: " + ", ".join(f"{k}({v})" for k, v in cats.most_common(3))
        )
    print("\n  A large skew means cluster coverage is systematic rather than random,")
    print("  so the fallback is not just handling a random remainder.")

    section("3. Candidate fallback keys, scored on the cluster-less records")
    print("  'Suspicious' = merged records whose requirements text disagrees, i.e.")
    print("  probably distinct roles sharing a title.\n")
    all_suspicious: dict[str, list[tuple[Any, list[JsonDict], float]]] = {}
    for name in candidate_keys(records[0]):
        groups: dict[Any, list[JsonDict]] = defaultdict(list)
        for record in without_cluster:
            groups[candidate_keys(record)[name]].append(record)
        report_groups(name, groups, baseline, threshold)
        multi = {k: v for k, v in groups.items() if len(v) > 1}
        all_suspicious[name] = [
            (k, v, s)
            for k, v in multi.items()
            if (s := group_similarity(v)) is not None and s < threshold
        ]

    section(f"4. Merges under the loosest key scoring below {threshold:.2f}, in full")
    worst = sorted(all_suspicious["(company, title)"], key=lambda item: item[2])
    if not worst:
        print("  None. The loosest key never merges records whose text disagrees.")
    for key, members, score in worst[: args.examples]:
        print(f"\n  similarity {score:.2f}   {key[0]} - {key[1]!r}   ({len(members)} records)")
        print(f"    varies: {describe(members)}")
        for member in members[:4]:
            job = sub(member, "v5_processed_job_data")
            print(
                f"    [{text(member.get('source')):18s}] "
                f"{text(job.get('estimated_publish_date'))[:10]}  "
                f"{'/'.join(c.split(',')[0] for c in cities(member))[:34]:34s} "
                f"req={text(member.get('requisition_id'))[:16]}"
            )

    section("Verdict")
    loose = all_suspicious["(company, title)"]
    tight = all_suspicious["(company, title, cities)"]
    print(f"  (company, title)          suspicious merges: {len(loose)}")
    print(f"  (company, title, cities)  suspicious merges: {len(tight)}")
    if baseline is not None:
        print(f"\n  Ground-truth mean is {baseline:.2f}. A fallback whose merged groups")
        print("  score at or above that is merging no more aggressively than the API.")
    print("\n  Read section 4 before choosing. If the suspicious groups are genuinely")
    print("  different roles, tighten the key. If they are the same job whose text was")
    print("  rewritten between postings, the looser key is right and the similarity")
    print("  threshold is what is wrong.")


if __name__ == "__main__":
    main()
