"""Tests for company-discovery stage 2 (rollup)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.company_discovery.rollup import (
    RollupOptions,
    rollup_records,
    run_rollup,
)

JsonDict = dict[str, Any]

# Arbitrary public reference point (Dulles Airport); see test_location_and_links.
ORIGIN_LAT, ORIGIN_LON = 38.9531, -77.4565
RESTON = {"lat": 38.9586, "lon": -77.3570}
COLLEGE_PARK = {"lat": 38.9897, "lon": -76.9378}
BOULDER = {"lat": 40.015, "lon": -105.2705}

OPTIONS = RollupOptions(
    home_latitude=ORIGIN_LAT,
    home_longitude=ORIGIN_LON,
    radius_miles=30.0,
    excluded_states=("Maryland",),
    excluded_sources=("usagov", "governmentjobs"),
    excluded_website_tlds=(".mil", ".gov", ".edu"),
)


def posting(
    *,
    object_id: str = "id-1",
    source: str = "grnhse",
    apply_url: str = "https://job-boards.greenhouse.io/acme/jobs/1",
    homepage: str | None = "acme.com",
    company_name: str = "Acme",
    enriched_name: str | None = "Acme",
    cities: list[str] | None = None,
    geoloc: list[dict[str, float]] | None = None,
    title: str = "Senior Software Engineer",
    category: str = "Software Development",
    published: str = "2026-08-01T00:00:00.000Z",
    expired: bool = False,
    enriched_extra: JsonDict | None = None,
) -> JsonDict:
    enriched: JsonDict = {"name": enriched_name, "nb_employees": 100}
    if homepage is not None:
        enriched["homepage_uri"] = homepage
    if enriched_extra:
        enriched.update(enriched_extra)
    return {
        "objectID": object_id,
        "source": source,
        "apply_url": apply_url,
        "is_expired": expired,
        "job_information": {"title": title},
        "v5_processed_job_data": {
            "core_job_title": title,
            "company_name": company_name,
            "job_category": category,
            "workplace_cities": cities if cities is not None else ["Reston, Virginia, US"],
            "estimated_publish_date": published,
        },
        "enriched_company_data": enriched,
        "_geoloc": geoloc if geoloc is not None else [RESTON],
    }


def roll(*records: JsonDict, options: RollupOptions = OPTIONS) -> JsonDict:
    return rollup_records(records, options)


# ----- grouping ----------------------------------------------------------


def test_postings_group_by_website_host() -> None:
    result = roll(
        posting(object_id="a", homepage="acme.com"),
        posting(object_id="b", homepage="https://www.Acme.com/careers"),
    )
    assert len(result["companies"]) == 1
    assert result["companies"][0]["posting_count"] == 2
    assert result["companies"][0]["website_host"] == "acme.com"


def test_company_name_variants_do_not_fragment_rows() -> None:
    """Per-posting names vary; the enriched name is the display name."""
    result = roll(
        posting(object_id="a", company_name="Acme Inc.", enriched_name="Acme"),
        posting(object_id="b", company_name="ACME Corporation", enriched_name="Acme"),
    )
    assert len(result["companies"]) == 1
    assert result["companies"][0]["name"] == "Acme"


def test_ats_host_is_not_used_as_company_identity() -> None:
    """Two employers sharing an ATS host must stay separate rows."""
    result = roll(
        posting(
            object_id="a",
            source="saashr",
            apply_url="https://secure7.saashr.com/ta/1.careers?ShowJob=1",
            homepage="vpdgov.com",
            enriched_name="VPD",
        ),
        posting(
            object_id="b",
            source="saashr",
            apply_url="https://secure7.saashr.com/ta/2.careers?ShowJob=2",
            homepage="othergov.com",
            enriched_name="Other",
        ),
    )
    assert {c["website_host"] for c in result["companies"]} == {"vpdgov.com", "othergov.com"}


# ----- drops -------------------------------------------------------------


def test_excluded_sources_are_dropped() -> None:
    result = roll(
        posting(object_id="a", source="usagov", apply_url="https://www.usajobs.gov/job/1"),
        posting(object_id="b"),
    )
    assert result["stats"]["dropped_excluded_source"] == 1
    assert len(result["companies"]) == 1


def test_public_sector_domains_are_dropped() -> None:
    """Source exclusion misses an agency on a mainstream ATS; the domain catches it."""
    result = roll(
        posting(object_id="a", source="workday", homepage="navy.mil", enriched_name="US Navy"),
        posting(object_id="b", source="workday", homepage="dc.gov", enriched_name="DC Gov"),
        posting(object_id="c", source="workday", homepage="psu.edu", enriched_name="Penn State"),
        posting(object_id="d", homepage="acme.com"),
    )
    assert result["stats"]["dropped_excluded_website_tld"] == 3
    assert [c["website_host"] for c in result["companies"]] == ["acme.com"]


def test_tld_exclusion_does_not_match_on_substrings() -> None:
    result = roll(posting(homepage="govtech.com"))
    assert [c["website_host"] for c in result["companies"]] == ["govtech.com"]


def test_tld_exclusion_accepts_suffixes_without_a_leading_dot() -> None:
    options = RollupOptions(
        home_latitude=ORIGIN_LAT,
        home_longitude=ORIGIN_LON,
        radius_miles=30.0,
        excluded_website_tlds=("mil",),
    )
    result = roll(posting(homepage="navy.mil"), options=options)
    assert result["companies"] == []


def test_website_less_records_survive_tld_exclusion() -> None:
    """There is no host to test, so the record still reaches the tail file."""
    result = roll(posting(homepage=None, enriched_name="No Site Co"))
    assert len(result["tail"]) == 1


def test_expired_postings_are_dropped() -> None:
    result = roll(posting(expired=True))
    assert result["stats"]["dropped_expired"] == 1
    assert result["companies"] == []


def test_maryland_only_posting_is_dropped() -> None:
    result = roll(posting(cities=["College Park, Maryland, US"], geoloc=[COLLEGE_PARK]))
    assert result["stats"]["dropped_no_qualifying_location"] == 1
    assert result["companies"] == []


def test_posting_far_from_home_is_dropped() -> None:
    result = roll(posting(cities=["Boulder, Colorado, US"], geoloc=[BOULDER]))
    assert result["stats"]["dropped_no_qualifying_location"] == 1


def test_distance_comes_from_the_nearest_qualifying_city() -> None:
    result = roll(
        posting(
            cities=[
                "Boulder, Colorado, US",
                "College Park, Maryland, US",
                "Reston, Virginia, US",
            ],
            geoloc=[BOULDER, COLLEGE_PARK, RESTON],
        )
    )
    company = result["companies"][0]
    assert company["locations"] == ["Reston, Virginia, US"]
    assert 4 < company["distance_miles"] < 8


def test_posting_without_location_is_kept_with_unknown_distance() -> None:
    result = roll(posting(cities=[], geoloc=[]))
    company = result["companies"][0]
    assert company["distance_miles"] is None
    assert company["locations"] == []
    assert result["stats"]["postings_without_location"] == 1
    assert result["stats"]["companies_without_distance"] == 1


# ----- company fields ----------------------------------------------------


def test_sample_titles_prefer_software_roles() -> None:
    result = roll(
        posting(object_id="a", title="Help Desk Technician", category="Information Technology"),
        posting(object_id="b", title="IT Support Lead", category="Information Technology"),
        posting(object_id="c", title="Staff Backend Engineer", category="Software Development"),
    )
    company = result["companies"][0]
    assert company["sample_titles"][0] == "Staff Backend Engineer"
    assert company["software_posting_count"] == 1
    assert company["category_counts"]["Information Technology"] == 2


def test_sample_titles_are_capped_and_deduplicated() -> None:
    result = roll(
        *[posting(object_id=str(i), title="Senior Software Engineer") for i in range(5)],
        posting(object_id="x", title="Platform Engineer"),
    )
    titles = result["companies"][0]["sample_titles"]
    assert titles == ["Senior Software Engineer", "Platform Engineer"]


def test_latest_published_date_is_the_newest() -> None:
    result = roll(
        posting(object_id="a", published="2026-06-01T00:00:00.000Z"),
        posting(object_id="b", published="2026-08-15T00:00:00.000Z"),
    )
    assert result["companies"][0]["latest_published_at"].startswith("2026-08-15")


def test_careers_link_follows_the_newest_posting() -> None:
    """When the link can only be a posting URL, prefer the newest one, since a
    stale posting is the one likely to have been delisted."""
    result = roll(
        posting(
            object_id="a",
            source="brassring",
            apply_url="https://sjobs.brassring.com/j?partnerid=1&jobid=OLD",
            published="2026-01-01T00:00:00.000Z",
        ),
        posting(
            object_id="b",
            source="brassring",
            apply_url="https://sjobs.brassring.com/j?partnerid=1&jobid=NEW",
            published="2026-08-01T00:00:00.000Z",
        ),
    )
    assert "NEW" in result["companies"][0]["careers_url"]


def test_board_tier_link_drops_the_job_id() -> None:
    result = roll(
        posting(
            object_id="a",
            source="adp",
            apply_url="https://workforcenow.adp.com/x?cid=1&jobId=593851&lang=en_US",
        )
    )
    company = result["companies"][0]
    assert company["careers_url"] == "https://workforcenow.adp.com/x?cid=1"
    assert company["careers_tier"] == "board"


def test_company_profile_is_carried_forward() -> None:
    result = roll(
        posting(
            enriched_extra={
                "tagline": "Builds widgets.",
                "industries": ["Widgets"],
                "year_founded": 2015,
                "latest_funding_type": "Series C",
            }
        )
    )
    profile = result["companies"][0]["profile"]
    assert profile["tagline"] == "Builds widgets."
    assert profile["industries"] == ["Widgets"]
    assert profile["year_founded"] == 2015
    assert profile["latest_funding_type"] == "Series C"


# ----- tail and diagnostics ---------------------------------------------


def test_records_without_a_website_go_to_the_tail() -> None:
    result = roll(
        posting(object_id="a", homepage=None, enriched_name="No Site Co"),
        posting(object_id="b", homepage="acme.com"),
    )
    assert len(result["companies"]) == 1
    assert len(result["tail"]) == 1
    tail = result["tail"][0]
    assert tail["has_website"] is False
    assert tail["website_url"] is None
    assert tail["careers_url"] == "https://job-boards.greenhouse.io/acme/"


def test_ambiguous_domains_are_reported() -> None:
    result = roll(
        posting(object_id="a", enriched_name="Acme"),
        posting(object_id="b", enriched_name="Acme Subsidiary"),
    )
    assert result["ambiguous_domains"]["acme.com"] == ["Acme", "Acme Subsidiary"]


def test_per_posting_name_variance_does_not_trigger_the_diagnostic() -> None:
    """Posting-text names are noisy ("Overview", legal suffixes); only the
    enrichment name is evidence of a questionable merge."""
    result = roll(
        posting(object_id="a", enriched_name="Acme", company_name="Acme, Inc."),
        posting(object_id="b", enriched_name="Acme", company_name="Overview"),
    )
    assert result["ambiguous_domains"] == {}
    assert result["companies"][0]["observed_names"] == ["Acme"]


def test_careers_tier_counts_are_reported() -> None:
    result = roll(
        posting(object_id="a", homepage="acme.com"),
        posting(
            object_id="b",
            source="bamboohr",
            apply_url="https://niyamit.bamboohr.com/careers/1",
            homepage="niyamit.com",
        ),
    )
    assert result["careers_tier_counts"] == {"host_and_segments": 1, "host": 1}


# ----- file output -------------------------------------------------------


def test_run_rollup_writes_files_and_meta(tmp_path: Path) -> None:
    records = [
        posting(object_id="a"),
        posting(object_id="b", homepage=None, enriched_name="No Site Co"),
        posting(object_id="c", source="usagov"),
    ]
    result = run_rollup(records, tmp_path, OPTIONS, source_path=Path("jobs.jsonl"))

    companies = [json.loads(line) for line in result.companies_path.read_text().splitlines()]
    tail = [json.loads(line) for line in result.tail_path.read_text().splitlines()]
    meta = json.loads(result.meta_path.read_text())

    assert len(companies) == 1 and len(tail) == 1
    assert result.company_count == 1 and result.tail_count == 1
    assert meta["source"] == "jobs.jsonl"
    assert meta["options"]["excluded_states"] == ["Maryland"]
    assert meta["options"]["excluded_website_tlds"] == [".mil", ".gov", ".edu"]
    assert meta["stats"]["postings_read"] == 3
    assert meta["stats"]["dropped_excluded_source"] == 1
