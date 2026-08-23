"""Stage 2 of job shortlist: flatten raw records and collapse duplicates.

Two jobs, both of which exist to keep the screen stage honest.

**Flattening.** Raw records nest their useful fields under
``v5_processed_job_data`` and ``enriched_company_data``. Screening rules that
reach into those paths break the moment the vendor renames a field, and they
break quietly: a renamed field reads as absent, an absent field fails a check,
and postings vanish with no error. Everything is projected onto a flat
``Posting`` instead, so a rename breaks one module loudly rather than the
filter logic silently.

**Duplicate collapsing.** ``liberal_dedup_cluster`` is hiring.cafe's own
cross-ATS duplicate key and is trusted wherever it appears, but it is present
on only about a third of records, so a fallback does most of the work.

The fallback is deliberately two-stage. A key of
``(company, title, cities)`` alone over-collapses: large employers post many
distinct roles under one generic title, and merging them means the second one
is never seen. Measured against records that *do* carry a cluster key, that
key's merges average around 0.53 text similarity where the API's own average
0.66. So the key only proposes candidates, and a similarity check on the
requirements text confirms each merge.

The confirmation threshold is calibrated per run from the cluster-keyed
records rather than hardcoded: those are known duplicates, so their similarity
distribution is what a real duplicate looks like in this data. A guessed
constant would reject merges the API itself makes - real duplicates score far
below 1.0 because the same job gets rewritten for each ATS.

Erring toward under-merging is deliberate. A missed merge shows the same job
twice and costs one glance; a wrong merge deletes a job you never see.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.common.jsonl import JsonlWriter, unique_path, with_compression
from hiringcafe_toolkit.common.urls import normalize_host

JsonDict = dict[str, Any]

#: Yearly figures outside this band are treated as unstated. The data contains
#: an $81,337,000 "yearly" salary and hourly rates mislabeled as yearly, and a
#: single bad figure would otherwise promote a posting on a number nobody wrote.
COMP_PLAUSIBLE_MIN = 25_000
COMP_PLAUSIBLE_MAX = 600_000

#: Percentile of ground-truth similarity used as the merge threshold. The weak
#: end of known-good merges, not their average: duplicates vary a lot, and a
#: stricter bar would split pairs the API itself joined.
CALIBRATION_PERCENTILE = 10

#: Used when a run has too few cluster groups to calibrate against.
DEFAULT_SIMILARITY = 0.44

#: Minimum ground-truth groups before calibration is trusted over the default.
MIN_CALIBRATION_GROUPS = 10

#: Cap on pairwise comparisons within one candidate group. Groups are tiny in
#: practice; this only bounds a pathological one.
MAX_PAIRS_PER_GROUP = 15

#: Sources that republish other boards' listings. When a duplicate group spans
#: several, the employer's own ATS is the better link to keep: an aggregator
#: entry is a copy and is likelier to be stale or to redirect.
AGGREGATOR_SOURCES = frozenset({"adhoc"})

WORD = re.compile(r"[a-z0-9+#.]+")

#: Seniority words and level numbers, read from the *raw* posting title.
#: ``core_job_title`` has these stripped upstream - measured at 208 raw titles
#: carrying a level against 32 core titles - so two requisitions at different
#: levels arrive with identical core titles, identical companies, identical
#: cities, and near-identical boilerplate requirements. Without this they merge
#: and the other levels are never seen.
LEVEL_WORD = re.compile(
    r"\b(?:junior|jr|associate|entry|mid|senior|snr|sr|staff|principal|lead|distinguished"
    r"|fellow|apprentice|intern|trainee)\b",
    re.I,
)

#: Numeric levels as a standalone trailing token: "Engineer II", "Engineer 3",
#: "Engineer 1.5", "Engineer L4". Anchored to avoid matching a "Tier 1" or a
#: "V" that is part of the role rather than its level.
LEVEL_NUMBER = re.compile(
    r"(?:^|[\s\-(,/])(?:l|t)?(i{1,3}|iv|vi{0,3}|[1-5](?:[._]\d)?)(?=$|[\s\-),/])",
    re.I,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Posting:
    """One job posting, flattened onto a schema this pipeline controls.

    Field names deliberately differ from the vendor's. The translation is done
    once, here, so that a change upstream is a change to one function.
    """

    # --- identity ---
    object_id: str
    cluster_key: str | None
    dedup_key: str
    """How this posting was grouped: the cluster key, or the fallback tuple."""
    fallback_key: str = ""
    """The candidate key this posting would group under, recorded even when a
    cluster key was used instead. Cluster coverage varies widely between runs -
    measured at 76% on one slice and 31% on another - so a posting can gain or
    lose its cluster key from one day to the next. Keeping both means a later
    stage can still recognize it."""
    duplicate_count: int = 1
    """Listings collapsed into this one, including itself."""
    alternate_object_ids: tuple[str, ...] = ()
    """Ids of the listings collapsed into this one.

    Which listing survives a merge depends on source preference and publish
    date, so a new listing arriving tomorrow can displace today's survivor and
    the same job would look new. Keeping every id lets a later stage match on
    any of them."""
    alternate_apply_urls: tuple[str, ...] = ()
    """Apply links from the collapsed listings, kept because a derived link is
    a heuristic and the surviving one occasionally 404s."""

    # --- role ---
    title: str = ""
    raw_title: str = ""
    """The posting's own title. Kept because ``core_job_title`` strips level
    markers, which is the only place a level distinction survives."""
    company: str = ""
    company_host: str | None = None
    job_category: str | None = None
    seniority_level: str | None = None
    """Vendor-inferred and unreliable: absent means extraction failed, not that
    the role is junior."""
    role_type: str | None = None
    employer_type: str | None = None
    min_years_experience: int | None = None
    """A *minimum*. A posting spanning mid to senior reports the low end, so
    this cannot be used to exclude."""

    # --- location ---
    workplace_type: str | None = None
    cities: tuple[str, ...] = ()
    city_count: int = 0
    """Postings spanning many cities carry geo-tiered pay bands, so their
    compensation ceiling is the priciest metro rather than a local figure."""

    # --- compensation ---
    comp_min: int | None = None
    comp_max: int | None = None
    comp_frequency: str | None = None
    comp_transparent: bool = False
    comp_implausible: bool = False
    """A figure was stated but fell outside the plausible band, so it was
    dropped. Distinct from a posting that stated nothing."""

    # --- screening inputs ---
    commitment: tuple[str, ...] = ()
    security_clearance: str | None = None
    """The structured field reads "None" on roles demanding TS/SCI, so
    ``certifications`` and ``requirements`` must be checked too."""
    certifications: tuple[str, ...] = ()
    requirements: str = ""
    tools: tuple[str, ...] = ()

    # --- provenance ---
    source: str = ""
    apply_url: str | None = None
    published_at: str | None = None
    is_expired: bool = False

    def as_record(self) -> JsonDict:
        return asdict(self)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Posting:
        """Rebuild a posting from its JSONL form.

        JSON has no tuples, so sequence fields arrive as lists and are
        converted back: later stages compare and hash these, and a list would
        fail at the point of use rather than at the point of loading.
        """
        known = {f.name for f in dataclass_fields(cls)}
        values: dict[str, Any] = {}
        for name in known & set(record):
            value = record[name]
            values[name] = tuple(value) if isinstance(value, list) else value
        unknown = set(record) - known
        if unknown:
            # Loud rather than silent: an unexpected key means the writer and
            # reader have drifted apart, which is the failure this schema
            # exists to prevent.
            raise ValueError(f"unknown posting fields: {', '.join(sorted(unknown))}")
        return cls(**values)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _optional(value: Any) -> str | None:
    return _text(value) or None


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(_text(item) for item in value if _text(item))


def _sub(record: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = record.get(key)
    return value if isinstance(value, Mapping) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _money(value: Any) -> tuple[int | None, bool]:
    """Return the figure and whether a stated one had to be discarded."""
    amount = _int(value)
    if amount is None:
        return None, False
    if COMP_PLAUSIBLE_MIN < amount < COMP_PLAUSIBLE_MAX:
        return amount, False
    return None, True


def normalized_title(title: str) -> str:
    """Whitespace- and case-normalized title, for grouping candidates."""
    return " ".join(title.lower().split())


def level_signature(raw_title: str) -> str:
    """Seniority and level tokens found in a raw posting title, sorted.

    Returned as a canonical string so two titles can be compared for level
    agreement without comparing their wording, which differs freely between
    listings of the same job ("Sr." against "Senior", a trailing department
    name, a parenthetical location).

    An empty signature means no level was stated, and that is treated as a
    level of its own: "Software Engineer" and "Senior Software Engineer" are
    different jobs, so declining to merge them is correct.
    """
    words = {match.group(0).lower().rstrip(".") for match in LEVEL_WORD.finditer(raw_title)}
    words = {"sr" if w in {"senior", "snr"} else "jr" if w == "junior" else w for w in words}
    numbers = {
        match.group(1).lower().replace("_", ".") for match in LEVEL_NUMBER.finditer(raw_title)
    }
    return "+".join(sorted(words | numbers))


def normalize_record(record: Mapping[str, Any]) -> Posting:
    """Project one raw record onto the flat schema."""
    job = _sub(record, "v5_processed_job_data")
    enriched = _sub(record, "enriched_company_data")

    comp_min, min_bad = _money(job.get("yearly_min_compensation"))
    comp_max, max_bad = _money(job.get("yearly_max_compensation"))
    cities = _strings(job.get("workplace_cities"))
    info = _sub(record, "job_information")
    raw_title = _text(info.get("job_title_raw")) or _text(info.get("title"))
    title = _text(job.get("core_job_title")) or raw_title
    company = _text(enriched.get("name")) or _text(job.get("company_name")) or "(unknown)"
    cluster = _optional(record.get("liberal_dedup_cluster"))

    return Posting(
        object_id=_text(record.get("objectID")) or _text(record.get("id")),
        cluster_key=cluster,
        dedup_key=cluster or "",  # settled during deduplication
        fallback_key=fallback_key(company, title, cities),
        title=title,
        raw_title=raw_title,
        company=company,
        company_host=normalize_host(_text(enriched.get("homepage_uri"))),
        job_category=_optional(job.get("job_category")),
        seniority_level=_optional(job.get("seniority_level")),
        role_type=_optional(job.get("role_type")),
        employer_type=_optional(job.get("position_employer_type")),
        min_years_experience=_int(job.get("min_industry_and_role_yoe")),
        workplace_type=_optional(job.get("workplace_type")),
        cities=cities,
        city_count=len(cities),
        comp_min=comp_min,
        comp_max=comp_max,
        comp_frequency=_optional(job.get("listed_compensation_frequency")),
        comp_transparent=job.get("is_compensation_transparent") is True,
        comp_implausible=min_bad or max_bad,
        commitment=_strings(job.get("commitment")),
        security_clearance=_optional(job.get("security_clearance")),
        certifications=_strings(job.get("licenses_or_certifications")),
        requirements=_text(job.get("requirements_summary")),
        tools=_strings(job.get("technical_tools")),
        source=_text(record.get("source")),
        apply_url=_optional(record.get("apply_url")),
        published_at=_optional(job.get("estimated_publish_date")),
        is_expired=record.get("is_expired") is True,
    )


# ----- duplicate detection ------------------------------------------------


def _tokens(value: str) -> frozenset[str]:
    return frozenset(WORD.findall(value.lower()))


def similarity(left: str, right: str) -> float:
    """Token overlap between two requirements summaries.

    Order-insensitive and cheap. Exact wording differs between listings of the
    same job, so what matters is shared vocabulary rather than phrasing.
    """
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _group_similarity(members: Sequence[Posting]) -> float | None:
    texts = [m.requirements for m in members if m.requirements]
    if len(texts) < 2:
        return None
    pairs = list(combinations(texts, 2))[:MAX_PAIRS_PER_GROUP]
    return statistics.fmean(similarity(a, b) for a, b in pairs)


def calibrate_similarity(postings: Sequence[Posting]) -> tuple[float, int]:
    """Derive the merge threshold from records the API already grouped.

    Returns the threshold and the number of ground-truth groups it came from,
    so a run that fell back to the default is visible in the meta rather than
    silently using a number that does not fit the data.
    """
    groups: dict[str, list[Posting]] = {}
    for posting in postings:
        if posting.cluster_key:
            groups.setdefault(posting.cluster_key, []).append(posting)

    scores = [
        score
        for members in groups.values()
        if len(members) > 1 and (score := _group_similarity(members)) is not None
    ]
    if len(scores) < MIN_CALIBRATION_GROUPS:
        return DEFAULT_SIMILARITY, len(scores)
    percentiles = statistics.quantiles(scores, n=100)
    return percentiles[CALIBRATION_PERCENTILE - 1], len(scores)


def fallback_key(company: str, title: str, cities: Sequence[str]) -> str:
    """Candidate key for records the API did not cluster.

    Takes components rather than a ``Posting`` so it can be computed while one
    is being built, and so a later stage can rebuild the key from stored fields
    without constructing a posting first.

    Source is deliberately excluded. A true cross-ATS duplicate appears under
    different sources by definition, so including it would prevent exactly the
    merges this key exists to make.
    """
    return "|".join((company, normalized_title(title), ",".join(sorted(cities))))


def _representative(members: Sequence[Posting]) -> Posting:
    """Pick which listing survives a merge.

    An employer's own ATS beats an aggregator copy, since the copy is likelier
    to be stale; a newer listing beats an older one; a fuller requirements
    summary breaks the remaining ties.
    """
    return min(
        members,
        key=lambda p: (
            p.source.lower() in AGGREGATOR_SOURCES,
            _negated(p.published_at or ""),
            -len(p.requirements),
        ),
    )


def _negated(value: str) -> tuple[int, ...]:
    """Sort key making a later ISO date compare smaller, without parsing."""
    return tuple(-ord(character) for character in value)


def _merge(members: Sequence[Posting], dedup_key: str) -> Posting:
    chosen = _representative(members)
    alternates = tuple(
        dict.fromkeys(m.apply_url for m in members if m.apply_url and m is not chosen)
    )
    other_ids = tuple(
        dict.fromkeys(m.object_id for m in members if m.object_id and m is not chosen)
    )
    return Posting(
        **{
            **asdict(chosen),
            "dedup_key": dedup_key,
            "duplicate_count": len(members),
            "alternate_apply_urls": alternates,
            "alternate_object_ids": other_ids,
        }
    )


def _confirm_subgroups(candidates: Sequence[Posting], threshold: float) -> list[list[Posting]]:
    """Split a candidate group into confirmed duplicate sets.

    Greedy: each posting joins the first subgroup whose representative it
    resembles closely enough, otherwise it starts its own. Postings with no
    requirements text cannot be compared, so they only ever join a subgroup
    when it is the sole candidate - unverifiable merges are declined.
    """
    subgroups: list[list[Posting]] = []
    for posting in candidates:
        level = level_signature(posting.raw_title)
        for subgroup in subgroups:
            anchor = subgroup[0]
            # Level variants of one role share a company, a city, a core title
            # and nearly all of their boilerplate, so text similarity alone
            # cannot separate them. The raw title can.
            if level != level_signature(anchor.raw_title):
                continue
            if not posting.requirements or not anchor.requirements:
                continue
            if similarity(posting.requirements, anchor.requirements) >= threshold:
                subgroup.append(posting)
                break
        else:
            subgroups.append([posting])
    return subgroups


def deduplicate(
    postings: Sequence[Posting], threshold: float
) -> tuple[list[Posting], dict[str, int]]:
    """Collapse duplicate listings. Returns the survivors and counts."""
    stats: Counter[str] = Counter()

    clustered: dict[str, list[Posting]] = {}
    unclustered: dict[str, list[Posting]] = {}
    for posting in postings:
        if posting.cluster_key:
            clustered.setdefault(posting.cluster_key, []).append(posting)
        else:
            unclustered.setdefault(posting.fallback_key, []).append(posting)

    kept: list[Posting] = []
    for key, members in clustered.items():
        stats["clustered_listings"] += len(members)
        if len(members) > 1:
            stats["merged_by_cluster_key"] += len(members) - 1
        kept.append(_merge(members, key))

    for key, members in unclustered.items():
        stats["unclustered_listings"] += len(members)
        if len(members) == 1:
            kept.append(_merge(members, key))
            continue
        stats["fallback_candidate_groups"] += 1
        subgroups = _confirm_subgroups(members, threshold)
        if len(subgroups) > 1:
            # The key grouped them; the text disagreed. These are the merges
            # that would have deleted a distinct role.
            stats["fallback_merges_declined"] += len(subgroups) - 1
        for index, subgroup in enumerate(subgroups):
            if len(subgroup) > 1:
                stats["merged_by_fallback"] += len(subgroup) - 1
            kept.append(_merge(subgroup, f"{key}#{index}" if index else key))

    stats["postings_in"] = len(postings)
    stats["postings_out"] = len(kept)
    return kept, dict(stats)


# ----- stage entry point --------------------------------------------------


@dataclass(frozen=True)
class NormalizeResult:
    postings_path: Path
    meta_path: Path
    postings_in: int
    postings_out: int
    threshold: float
    stats: JsonDict = field(default_factory=dict)


def run_normalize(
    records: Iterable[Mapping[str, Any]],
    out_dir: Path,
    *,
    source_path: Path | None = None,
    compress: bool = True,
    threshold: float | None = None,
) -> NormalizeResult:
    """Flatten and deduplicate a raw scrape, writing postings plus a sidecar."""
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    postings_path = unique_path(with_compression(out_dir / f"postings-{stamp}.jsonl", compress))
    meta_path = unique_path(out_dir / f"normalize-meta-{stamp}.json")

    postings = [normalize_record(record) for record in records]
    calibrated, ground_truth_groups = calibrate_similarity(postings)
    effective = calibrated if threshold is None else threshold

    kept, stats = deduplicate(postings, effective)
    kept.sort(key=lambda p: (_negated(p.published_at or ""), p.company, p.title))

    with JsonlWriter(postings_path) as writer:
        for posting in kept:
            writer.write(posting.as_record())

    meta: JsonDict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": str(source_path) if source_path else None,
        "postings_file": postings_path.name,
        "compressed": compress,
        "similarity_threshold": round(effective, 4),
        "similarity_calibrated": round(calibrated, 4),
        "similarity_overridden": threshold is not None,
        "ground_truth_groups": ground_truth_groups,
        "cluster_key_coverage": round(sum(1 for p in postings if p.cluster_key) / len(postings), 4)
        if postings
        else 0.0,
        "stats": stats,
        "comp_implausible": sum(1 for p in kept if p.comp_implausible),
        "without_requirements_text": sum(1 for p in kept if not p.requirements),
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(
        "normalized %d records into %d postings (threshold %.2f from %d groups)",
        len(postings),
        len(kept),
        effective,
        ground_truth_groups,
    )
    return NormalizeResult(
        postings_path=postings_path,
        meta_path=meta_path,
        postings_in=len(postings),
        postings_out=len(kept),
        threshold=effective,
        stats=stats,
    )
