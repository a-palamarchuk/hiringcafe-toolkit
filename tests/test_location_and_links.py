"""Tests for location filtering and careers-link derivation."""

from __future__ import annotations

import pytest

from hiringcafe_toolkit.common.location import (
    build_places,
    haversine_miles,
    normalize_states,
    parse_state,
    select_nearby_places,
)
from hiringcafe_toolkit.common.urls import host_to_url, normalize_host
from hiringcafe_toolkit.company_discovery.careers_link import (
    TIER_RANK,
    DerivationTier,
    derive_careers_link,
)

# Arbitrary public reference point (Dulles Airport). These tests need stable
# geography - a Virginia city in range, a Maryland one excluded, a Colorado one
# far away - not anyone's actual home.
ORIGIN = (38.9531, -77.4565)
RESTON = {"lat": 38.9586, "lon": -77.3570}
COLLEGE_PARK = {"lat": 38.9897, "lon": -76.9378}
BOULDER = {"lat": 40.015, "lon": -105.2705}
MD = normalize_states(["Maryland"])


def nearby(cities: list[str], geoloc: list[dict[str, float]], radius: float = 30.0) -> list[str]:
    places = build_places(cities, geoloc)
    kept = select_nearby_places(
        places,
        home_latitude=ORIGIN[0],
        home_longitude=ORIGIN[1],
        radius_miles=radius,
        excluded_states=MD,
    )
    return [place.label for place in kept]


# ----- distance ----------------------------------------------------------


def test_haversine_matches_known_distance() -> None:
    # Dulles Airport to Reston VA is roughly 5 miles.
    assert haversine_miles(*ORIGIN, RESTON["lat"], RESTON["lon"]) == pytest.approx(5.4, abs=1.0)


def test_haversine_is_zero_for_same_point() -> None:
    assert haversine_miles(*ORIGIN, *ORIGIN) == pytest.approx(0.0)


# ----- state parsing -----------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("College Park, Maryland, US", "Maryland"),
        ("Toronto, Ontario, CA", "Ontario"),
        ("Washington, District of Columbia, US", "District of Columbia"),
        ("Remote", None),
        ("Berlin, DE", None),
    ],
)
def test_parse_state(label: str, expected: str | None) -> None:
    assert parse_state(label) == expected


# ----- per-city selection ------------------------------------------------


def test_multi_city_posting_survives_on_its_virginia_city() -> None:
    """A posting listing both Maryland and Virginia keeps the Virginia one."""
    kept = nearby(["College Park, Maryland, US", "Reston, Virginia, US"], [COLLEGE_PARK, RESTON])
    assert kept == ["Reston, Virginia, US"]


def test_maryland_only_posting_is_dropped() -> None:
    assert nearby(["College Park, Maryland, US"], [COLLEGE_PARK]) == []


def test_out_of_radius_cities_are_dropped() -> None:
    """The scrape matches on any location, so far-away cities must not count."""
    kept = nearby(["Boulder, Colorado, US", "Reston, Virginia, US"], [BOULDER, RESTON])
    assert kept == ["Reston, Virginia, US"]


def test_kept_places_are_sorted_nearest_first() -> None:
    far = {"lat": 38.8816, "lon": -77.0910}  # Arlington
    kept = nearby(["Arlington, Virginia, US", "Reston, Virginia, US"], [far, RESTON])
    assert kept[0] == "Reston, Virginia, US"


def test_city_without_coordinates_is_kept_with_unknown_distance() -> None:
    places = build_places(["Fairfax, Virginia, US"], [])
    kept = select_nearby_places(
        places,
        home_latitude=ORIGIN[0],
        home_longitude=ORIGIN[1],
        radius_miles=30.0,
        excluded_states=MD,
    )
    assert [p.label for p in kept] == ["Fairfax, Virginia, US"]
    assert kept[0].distance_miles is None


def test_city_without_coordinates_still_honors_state_exclusion() -> None:
    places = build_places(["Rockville, Maryland, US"], [])
    kept = select_nearby_places(
        places,
        home_latitude=ORIGIN[0],
        home_longitude=ORIGIN[1],
        radius_miles=30.0,
        excluded_states=MD,
    )
    assert kept == []


def test_build_places_pairs_by_index() -> None:
    places = build_places(
        ["Reston, Virginia, US", "College Park, Maryland, US"], [RESTON, COLLEGE_PARK]
    )
    assert places[0].latitude == RESTON["lat"]
    assert places[1].state == "Maryland"


def test_build_places_tolerates_short_geoloc_list() -> None:
    places = build_places(["A, Virginia, US", "B, Virginia, US"], [RESTON])
    assert places[1].latitude is None


def test_build_places_accepts_lng_spelling() -> None:
    places = build_places(["A, Virginia, US"], [{"lat": 38.9, "lng": -77.3}])
    assert places[0].longitude == pytest.approx(-77.3)


# ----- host normalization ------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ionq.com", "ionq.com"),
        ("www.IonQ.com", "ionq.com"),
        ("https://www.vpdgov.com/careers", "vpdgov.com"),
        ("  ionq.com  ", "ionq.com"),
        ("ionq.com:8080", "ionq.com"),
        ("localhost", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_host(value: str | None, expected: str | None) -> None:
    assert normalize_host(value) == expected


def test_host_to_url() -> None:
    assert host_to_url("ionq.com") == "https://ionq.com"


# ----- careers link ------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "source", "expected", "tier"),
    [
        (
            "https://job-boards.greenhouse.io/ionq/jobs/6143087004",
            "grnhse",
            "https://job-boards.greenhouse.io/ionq/",
            DerivationTier.HOST_AND_SEGMENTS,
        ),
        (
            "https://jobs.lever.co/agile-defense/fa6cdbda",
            "lever",
            "https://jobs.lever.co/agile-defense/",
            DerivationTier.HOST_AND_SEGMENTS,
        ),
        (
            "https://aero.wd5.myworkdayjobs.com/external/job/Washington-DC/Expert_R016048",
            "workday",
            "https://aero.wd5.myworkdayjobs.com/external/",
            DerivationTier.HOST_AND_SEGMENTS,
        ),
        (
            "https://niyamit.bamboohr.com/careers/424",
            "bamboohr",
            "https://niyamit.bamboohr.com/",
            DerivationTier.HOST,
        ),
        (
            "https://careers-iridium.icims.com/jobs/5029/job?utm_source=x",
            "icims",
            "https://careers-iridium.icims.com/",
            DerivationTier.HOST,
        ),
        (
            "https://www.amazon.jobs/en/jobs/10506766/dco-tech-3-dco",
            "adhoc",
            "https://www.amazon.jobs/",
            DerivationTier.HOST,
        ),
        (
            "https://some-new-ats.example.com/jobs/123",
            "brand_new_ats",
            "https://some-new-ats.example.com/",
            DerivationTier.HOST,
        ),
    ],
)
def test_derive_careers_link_trims_to_board(
    url: str, source: str, expected: str, tier: DerivationTier
) -> None:
    link = derive_careers_link(url, source)
    assert link.url == expected
    assert link.tier is tier


@pytest.mark.parametrize(
    ("url", "source", "expected"),
    [
        (
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            "?cid=abc&ccId=19000101_000001&jobId=593851&lang=en_US",
            "adp",
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            "?cid=abc&ccId=19000101_000001",
        ),
        (
            "https://www.paycomonline.net/v4/ats/web.php/jobs/ViewJobDetails?job=1&clientkey=k",
            "paycom",
            "https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=k",
        ),
        (
            "https://recruitingbypaycor.com/career/JobIntroduction.action?clientId=c&id=x&lang=en",
            "paycor",
            "https://recruitingbypaycor.com/career/CareerHome.action?clientId=c",
        ),
        (
            "https://secure7.saashr.com/ta/6203160.careers?ShowJob=621251759",
            "saashr",
            "https://secure7.saashr.com/ta/6203160.careers",
        ),
        (
            "https://phh.tbe.taleo.net/phh03/ats/careers/requisition.jsp?org=X&cws=37&rid=3697",
            "taleo_rss",
            "https://phh.tbe.taleo.net/phh03/ats/careers/jobSearch.jsp?org=X&cws=37",
        ),
    ],
)
def test_board_url_is_rebuilt_from_the_employer_parameter(
    url: str, source: str, expected: str
) -> None:
    """The employer parameter survives; the job id does not."""
    link = derive_careers_link(url, source)
    assert link.url == expected
    assert link.tier is DerivationTier.BOARD


@pytest.mark.parametrize(
    ("url", "source"),
    [
        (
            "https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad"
            "?partnerid=25397&siteid=5259&PageType=JobDetails&jobid=623192",
            "brassring",
        ),
        ("https://portal.brightmove.com/jb.do?reqGK=27783385&companyGK=48874", "brightmove"),
    ],
)
def test_query_param_ats_without_a_board_rule_keeps_the_posting_url(url: str, source: str) -> None:
    """Trimming these to a host lands on a vendor page, not the employer's board."""
    link = derive_careers_link(url, source)
    assert link.url == url
    assert link.tier is DerivationTier.POSTING


@pytest.mark.parametrize(
    ("url", "source"),
    [(None, "grnhse"), ("", "grnhse"), ("not a url", "grnhse"), ("ftp://x.com/j", "grnhse")],
)
def test_derive_careers_link_rejects_unusable_input(url: str | None, source: str) -> None:
    link = derive_careers_link(url, source)
    assert link.url is None
    assert link.tier is DerivationTier.NONE


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://eihu.fa.us8.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX"
            "/requisitions/job/2615878",
            "https://eihu.fa.us8.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX"
            "/requisitions",
        ),
        (
            # Site name varies, and the job segment is sometimes not nested
            # under requisitions.
            "https://fa-essf-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en"
            "/sites/CX_1/job/10003953",
            "https://fa-essf-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en"
            "/sites/CX_1/requisitions",
        ),
    ],
)
def test_oracle_cloud_trims_to_the_site_root(url: str, expected: str) -> None:
    link = derive_careers_link(url, "oraclecloud")
    assert link.url == expected
    assert link.tier is DerivationTier.HOST_AND_SEGMENTS


def test_oracle_cloud_without_a_site_segment_keeps_the_posting() -> None:
    """A shape the rule does not recognize must not produce a confident guess."""
    link = derive_careers_link("https://x.oraclecloud.com/hcmUI/nothing/here", "oraclecloud")
    assert link.url == "https://x.oraclecloud.com/hcmUI/nothing/here"
    assert link.tier is DerivationTier.POSTING


def test_tier_rank_prefers_job_lists_over_postings() -> None:
    assert TIER_RANK[DerivationTier.HOST] == TIER_RANK[DerivationTier.HOST_AND_SEGMENTS]
    assert TIER_RANK[DerivationTier.HOST] < TIER_RANK[DerivationTier.BOARD]
    assert TIER_RANK[DerivationTier.BOARD] < TIER_RANK[DerivationTier.POSTING]
    assert TIER_RANK[DerivationTier.POSTING] < TIER_RANK[DerivationTier.NONE]


def test_path_source_without_enough_segments_falls_back_to_host() -> None:
    link = derive_careers_link("https://jobs.lever.co/", "lever")
    assert link.url == "https://jobs.lever.co/"
    assert link.tier is DerivationTier.HOST
