"""Stage 2 of company discovery: roll postings up into companies.

The output unit of this pipeline is a company career page to visit, not a job
posting. This stage does the conversion: it drops postings that do not qualify,
groups the rest by company, and computes the fields the rendered list needs.

Location filtering lives here rather than in a later stage because it is the
same work as computing distance. Deciding which of a posting's workplace cities
count is what produces the distance number, so splitting the two would leave
distance ambiguous until after filtering.

Companies are keyed on the normalized website host from
``enriched_company_data.homepage_uri``. The per-posting
``v5_processed_job_data.company_website`` is deliberately ignored: it sometimes
holds the ATS host (``secure7.saashr.com``), which would merge every unrelated
employer on that ATS into one row. Records with no usable homepage are keyed on
their derived careers link instead and marked, since they cannot be
deduplicated against a browsing history that is recorded by company domain.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.common.careers_link import (
    TIER_RANK,
    DerivationTier,
    derive_careers_link,
)
from hiringcafe_toolkit.common.jsonl import JsonlWriter
from hiringcafe_toolkit.common.location import (
    Place,
    build_places,
    normalize_states,
    select_nearby_places,
)
from hiringcafe_toolkit.common.urls import host_to_url, normalize_host

JsonDict = dict[str, Any]

SOFTWARE_JOB_CATEGORY = "Software Development"
MAX_SAMPLE_TITLES = 3
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RollupOptions:
    """Inputs to a rollup run."""

    home_latitude: float
    home_longitude: float
    radius_miles: float
    excluded_states: tuple[str, ...] = ()
    excluded_sources: tuple[str, ...] = ()
    excluded_website_tlds: tuple[str, ...] = ()


@dataclass
class CompanyAccumulator:
    """Mutable per-company state while scanning postings."""

    key: str
    has_website: bool
    name: str | None = None
    website_host: str | None = None
    careers_url: str | None = None
    careers_tier: DerivationTier = DerivationTier.NONE
    careers_published_at: str | None = None
    posting_count: int = 0
    category_counts: Counter[str] = field(default_factory=Counter)
    latest_published_at: str | None = None
    observed_names: set[str] = field(default_factory=set)
    """Distinct company names seen from *enrichment*, for the ambiguity check."""
    sources: set[str] = field(default_factory=set)
    places: dict[str, Place] = field(default_factory=dict)
    software_titles: list[tuple[str, str]] = field(default_factory=list)
    other_titles: list[tuple[str, str]] = field(default_factory=list)
    profile: JsonDict = field(default_factory=dict)


def _negated(published: str) -> tuple[int, ...]:
    """Sort key making a newer date compare smaller.

    Dates are ISO strings, so this inverts them codepoint-wise rather than
    parsing, which keeps missing and malformed values orderable.
    """
    return tuple(-ord(character) for character in published)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _sequence(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _company_profile(enriched: Mapping[str, Any]) -> JsonDict:
    """Carry forward company attributes that later filtering may want.

    These are not all rendered. Re-deriving them would mean re-running the
    rollup, so they ride along in the interim data where a later filter (drop
    tiny companies, drop staffing firms by industry) can reach them.
    """
    fields = (
        "tagline",
        "industries",
        "activities",
        "nb_employees",
        "year_founded",
        "organization_type",
        "hq_country",
        "parent_company",
        "latest_funding_type",
        "latest_funding_year",
        "latest_funding_amount",
        "latest_funding_investors",
        "stock_exchange",
        "stock_symbol",
    )
    profile = {name: enriched[name] for name in fields if enriched.get(name) is not None}
    return profile


def _merge_places(accumulator: CompanyAccumulator, places: Iterable[Place]) -> None:
    for place in places:
        existing = accumulator.places.get(place.label)
        if existing is None:
            accumulator.places[place.label] = place
            continue
        if existing.distance_miles is None and place.distance_miles is not None:
            accumulator.places[place.label] = place


def _record_title(
    accumulator: CompanyAccumulator, title: str, category: str, published: str
) -> None:
    bucket = (
        accumulator.software_titles
        if category == SOFTWARE_JOB_CATEGORY
        else accumulator.other_titles
    )
    bucket.append((published, title))


def _sample_titles(accumulator: CompanyAccumulator) -> list[str]:
    """Prefer software titles, newest first, then fall back to other roles.

    At a company with fifteen IT postings and two software ones, the software
    titles are what tell you whether the visit is worth making.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for bucket in (accumulator.software_titles, accumulator.other_titles):
        for _published, title in sorted(bucket, key=lambda item: item[0], reverse=True):
            if title not in seen:
                seen.add(title)
                ordered.append(title)
            if len(ordered) >= MAX_SAMPLE_TITLES:
                return ordered
    return ordered


def _finalize(accumulator: CompanyAccumulator) -> JsonDict:
    places = sorted(
        accumulator.places.values(),
        key=lambda p: (p.distance_miles is None, p.distance_miles or 0.0),
    )
    distances = [p.distance_miles for p in places if p.distance_miles is not None]
    record: JsonDict = {
        "key": accumulator.key,
        "name": accumulator.name or accumulator.website_host or accumulator.key,
        "website_host": accumulator.website_host,
        "website_url": host_to_url(accumulator.website_host) if accumulator.website_host else None,
        "has_website": accumulator.has_website,
        "careers_url": accumulator.careers_url,
        "careers_tier": accumulator.careers_tier.value,
        "distance_miles": round(min(distances), 2) if distances else None,
        "locations": [p.label for p in places],
        "posting_count": accumulator.posting_count,
        "category_counts": dict(sorted(accumulator.category_counts.items())),
        "software_posting_count": accumulator.category_counts.get(SOFTWARE_JOB_CATEGORY, 0),
        "sample_titles": _sample_titles(accumulator),
        "latest_published_at": accumulator.latest_published_at,
        "sources": sorted(accumulator.sources),
        "observed_names": sorted(accumulator.observed_names),
        "profile": accumulator.profile,
    }
    return record


def rollup_records(records: Iterable[Mapping[str, Any]], options: RollupOptions) -> JsonDict:
    """Aggregate raw postings into per-company records.

    Returns a dict with ``companies``, ``tail`` (companies with no usable
    website, which cannot be deduplicated against the visit log), and ``stats``.
    """
    excluded_states = normalize_states(options.excluded_states)
    excluded_sources = {source.strip().lower() for source in options.excluded_sources}
    excluded_tlds = tuple(
        suffix if suffix.startswith(".") else f".{suffix}"
        for suffix in (raw.strip().lower() for raw in options.excluded_website_tlds)
        if suffix
    )

    accumulators: dict[str, CompanyAccumulator] = {}
    stats: Counter[str] = Counter()

    for record in records:
        stats["postings_read"] += 1

        source = (_text(record.get("source")) or "").lower()
        if source in excluded_sources:
            stats["dropped_excluded_source"] += 1
            continue
        if record.get("is_expired") is True:
            stats["dropped_expired"] += 1
            continue

        job_data = record.get("v5_processed_job_data")
        job_data = job_data if isinstance(job_data, Mapping) else {}
        enriched = record.get("enriched_company_data")
        enriched = enriched if isinstance(enriched, Mapping) else {}

        website_host = normalize_host(_text(enriched.get("homepage_uri")))
        if website_host is not None and website_host.endswith(excluded_tlds):
            stats["dropped_excluded_website_tld"] += 1
            continue

        cities = _sequence(job_data.get("workplace_cities"))
        places = build_places(cities, _sequence(record.get("_geoloc")))
        if places:
            kept_places = select_nearby_places(
                places,
                home_latitude=options.home_latitude,
                home_longitude=options.home_longitude,
                radius_miles=options.radius_miles,
                excluded_states=excluded_states,
            )
            if not kept_places:
                stats["dropped_no_qualifying_location"] += 1
                continue
        else:
            # No location data at all: kept deliberately, with unknown
            # distance, rather than dropped on missing evidence.
            kept_places = []
            stats["postings_without_location"] += 1

        apply_url = _text(record.get("apply_url"))
        careers = derive_careers_link(apply_url, source)

        if website_host is not None:
            key = website_host
            has_website = True
        elif careers.url is not None:
            key = careers.url
            has_website = False
            stats["postings_without_website"] += 1
        else:
            stats["dropped_unidentifiable"] += 1
            continue

        accumulator = accumulators.get(key)
        if accumulator is None:
            accumulator = CompanyAccumulator(key=key, has_website=has_website)
            accumulators[key] = accumulator

        accumulator.posting_count += 1
        accumulator.website_host = accumulator.website_host or website_host
        if source:
            accumulator.sources.add(source)

        enriched_name = _text(enriched.get("name"))
        posting_name = _text(job_data.get("company_name"))
        accumulator.name = accumulator.name or enriched_name or posting_name
        # Only enrichment-provided names are tracked for the ambiguity check.
        # The per-posting name is extracted from posting text and is unreliable
        # as identity - it yields spelling variants, legal-suffix noise, and
        # outright junk ("Overview", "Company Description") - so including it
        # made the diagnostic fire on half the dataset.
        if enriched_name:
            accumulator.observed_names.add(enriched_name)

        if not accumulator.profile and enriched:
            accumulator.profile = _company_profile(enriched)

        _merge_places(accumulator, kept_places)

        category = _text(job_data.get("job_category")) or "Unknown"
        accumulator.category_counts[category] += 1

        published = _text(job_data.get("estimated_publish_date")) or ""
        if published and (
            accumulator.latest_published_at is None or published > accumulator.latest_published_at
        ):
            accumulator.latest_published_at = published

        title = _text(job_data.get("core_job_title"))
        job_information = record.get("job_information")
        if title is None and isinstance(job_information, Mapping):
            title = _text(job_information.get("title"))
        if title:
            _record_title(accumulator, title, category, published)

        # Prefer the link that lands closest to a job list, then the newest.
        # A company posting through two applicant tracking systems should get
        # the one with a usable board URL rather than whichever posting was
        # most recent; recency only breaks ties, since a stale posting link may
        # already be delisted.
        if careers.url is not None:
            candidate = (TIER_RANK[careers.tier], _negated(published))
            current = (
                TIER_RANK[accumulator.careers_tier],
                _negated(accumulator.careers_published_at or ""),
            )
            if accumulator.careers_url is None or candidate < current:
                accumulator.careers_url = careers.url
                accumulator.careers_tier = careers.tier
                accumulator.careers_published_at = published

    companies: list[JsonDict] = []
    tail: list[JsonDict] = []
    tier_counts: Counter[str] = Counter()
    for accumulator in accumulators.values():
        finalized = _finalize(accumulator)
        tier_counts[finalized["careers_tier"]] += 1
        if accumulator.has_website:
            companies.append(finalized)
        else:
            tail.append(finalized)

    stats["companies"] = len(companies)
    stats["tail_companies"] = len(tail)
    stats["companies_without_distance"] = sum(
        1 for company in companies if company["distance_miles"] is None
    )

    # Domains that enrichment mapped to more than one company name. Shared
    # domains across a parent and its subsidiaries are expected and correct
    # here (one domain, one careers page to visit); this exists to surface a
    # domain that merged genuinely unrelated employers.
    ambiguous = {
        company["website_host"]: company["observed_names"]
        for company in companies
        if len(company["observed_names"]) > 1
    }

    return {
        "companies": companies,
        "tail": tail,
        "stats": dict(stats),
        "careers_tier_counts": dict(tier_counts),
        "ambiguous_domains": ambiguous,
    }


@dataclass(frozen=True)
class RollupResult:
    companies_path: Path
    tail_path: Path
    meta_path: Path
    company_count: int
    tail_count: int
    stats: JsonDict


def run_rollup(
    records: Iterable[Mapping[str, Any]],
    out_dir: Path,
    options: RollupOptions,
    *,
    source_path: Path | None = None,
) -> RollupResult:
    """Roll postings up and write company records plus a meta sidecar."""
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    companies_path = out_dir / f"companies-{stamp}.jsonl"
    tail_path = out_dir / f"companies-no-website-{stamp}.jsonl"
    meta_path = out_dir / f"rollup-meta-{stamp}.json"

    result = rollup_records(records, options)

    with JsonlWriter(companies_path) as writer:
        for company in result["companies"]:
            writer.write(company)
    with JsonlWriter(tail_path) as writer:
        for company in result["tail"]:
            writer.write(company)

    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": str(source_path) if source_path else None,
        "options": {
            "home_latitude": options.home_latitude,
            "home_longitude": options.home_longitude,
            "radius_miles": options.radius_miles,
            "excluded_states": list(options.excluded_states),
            "excluded_sources": list(options.excluded_sources),
            "excluded_website_tlds": list(options.excluded_website_tlds),
        },
        "stats": result["stats"],
        "careers_tier_counts": result["careers_tier_counts"],
        "ambiguous_domains": result["ambiguous_domains"],
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    return RollupResult(
        companies_path=companies_path,
        tail_path=tail_path,
        meta_path=meta_path,
        company_count=len(result["companies"]),
        tail_count=len(result["tail"]),
        stats=result["stats"],
    )
