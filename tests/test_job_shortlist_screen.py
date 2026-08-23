"""Tests for job-shortlist stage 3.

Weighted toward the two failure modes that cost real postings: a hard reject
firing on an unreliable signal, and a reason going unrecorded so the discard
cannot be audited.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.common.config import ScreenSettings
from hiringcafe_toolkit.common.jsonl import read_jsonl
from hiringcafe_toolkit.job_shortlist.normalize import Posting
from hiringcafe_toolkit.job_shortlist.screen import (
    BAND_POSSIBLE,
    BAND_REJECTED,
    BAND_STRONG,
    assign_band,
    demote_reasons,
    manages_people,
    reject_reasons,
    run_screen,
    title_says_senior,
)

SETTINGS = ScreenSettings()

BACKEND = ("Java", "Spring Boot", "Kubernetes", "AWS")


def posting(**overrides: Any) -> Posting:
    """A posting that lands in `strong` unless an override pushes it out."""
    defaults: dict[str, Any] = {
        "object_id": "a",
        "cluster_key": None,
        "dedup_key": "k",
        "title": "Software Engineer",
        "raw_title": "Senior Software Engineer",
        "company": "Acme",
        "company_host": "acme.com",
        "seniority_level": "Senior Level",
        "role_type": "Individual Contributor",
        "employer_type": "Internal Position",
        "cities": ("Reston, Virginia, US",),
        "city_count": 1,
        "comp_min": 190_000,
        "comp_max": 220_000,
        "commitment": ("Full Time",),
        "security_clearance": "None",
        "tools": BACKEND,
        "requirements": "Builds distributed backend services.",
        "source": "grnhse",
    }
    return Posting(**{**defaults, **overrides})


def band(**overrides: Any) -> str:
    return assign_band(posting(**overrides), SETTINGS).band


# ----- baseline -----------------------------------------------------------


def test_a_senior_backend_posting_is_strong() -> None:
    assert band() == BAND_STRONG


# ----- hard rejects -------------------------------------------------------


def test_hard_rejects_fire_on_facts() -> None:
    assert band(is_expired=True) == BAND_REJECTED
    assert band(source="usagov") == BAND_REJECTED
    assert band(company_host="nasa.gov") == BAND_REJECTED
    assert band(commitment=("Contract",)) == BAND_REJECTED
    assert band(employer_type="External Position") == BAND_REJECTED
    assert band(security_clearance="Top Secret") == BAND_REJECTED
    assert band(title="Full Stack Engineer") == BAND_REJECTED


def test_clearance_is_caught_in_text_when_the_field_says_none() -> None:
    """The field reads "None" on roles demanding TS/SCI, so text is the guarantee."""
    assert "clearance in text" in reject_reasons(
        posting(security_clearance="None", certifications=("ts/sci", "ci polygraph")), SETTINGS
    )
    assert "clearance in text" in reject_reasons(
        posting(requirements="Requires an active DoD Secret clearance."), SETTINGS
    )


def test_clearance_in_the_raw_title_is_caught() -> None:
    """The core title is normalized, so "Mission Engineer, Cleared" loses the word."""
    verdict = assign_band(
        posting(title="Mission Engineer", raw_title="Mission Engineer, Cleared"), SETTINGS
    )

    assert verdict.band == BAND_REJECTED
    assert "clearance in text" in verdict.reject_reasons


def test_non_software_tools_reject_only_without_software_tools() -> None:
    assert band(tools=("SolidWorks", "AutoCAD", "Revit")) == BAND_REJECTED
    assert band(tools=("SolidWorks", "Python", "Kubernetes")) != BAND_REJECTED


def test_blocked_companies_are_rejected_case_insensitively() -> None:
    settings = ScreenSettings(company_blocklist=frozenset({"acme"}))

    assert assign_band(posting(company="ACME"), settings).band == BAND_REJECTED
    assert assign_band(posting(company="Acme"), SETTINGS).band == BAND_STRONG


# ----- compensation -------------------------------------------------------


def test_stated_comp_below_the_floor_is_rejected() -> None:
    assert band(comp_max=120_000) == BAND_REJECTED


def test_unstated_comp_is_never_rejected() -> None:
    """Postings stating nothing are a large and good slice of the results."""
    assert band(comp_max=None, comp_min=None) == BAND_STRONG


def test_a_low_band_bottom_demotes_rather_than_rejects() -> None:
    """A wide band means the ceiling may belong to another level or metro."""
    verdict = assign_band(posting(comp_min=90_000, comp_max=220_000), SETTINGS)

    assert verdict.band == BAND_POSSIBLE
    assert "band bottom below floor" in verdict.demote_reasons
    assert verdict.reject_reasons == ()


def test_many_cities_demotes_for_geo_tiered_pay() -> None:
    verdict = assign_band(posting(city_count=42), SETTINGS)

    assert verdict.band == BAND_POSSIBLE
    assert "geo-tiered pay band" in verdict.demote_reasons


# ----- role_type ----------------------------------------------------------


def test_people_manager_rejects_only_when_corroborated() -> None:
    corroborated = posting(role_type="People Manager", title="Engineering Manager")
    by_text = posting(
        role_type="People Manager", requirements="Leads a team of 8 with direct reports."
    )

    assert "people manager" in reject_reasons(corroborated, SETTINGS)
    assert "people manager" in reject_reasons(by_text, SETTINGS)
    assert manages_people(corroborated)


def test_an_ic_ladder_title_flagged_as_manager_is_demoted_not_rejected() -> None:
    """The flag reads "Principal Engineer" as management on real IC postings."""
    verdict = assign_band(
        posting(
            role_type="People Manager",
            title="Principal Engineer",
            raw_title="Principal Engineer-Software Development",
        ),
        SETTINGS,
    )

    assert verdict.band == BAND_POSSIBLE
    assert "uncorroborated people-manager flag" in verdict.demote_reasons
    assert verdict.reject_reasons == ()


# ----- seniority ----------------------------------------------------------


def test_the_raw_title_can_supply_seniority_the_field_missed() -> None:
    """seniority_level calls "Senior ... Engineer" entry level about 1 in 7 times."""
    assert title_says_senior(posting(raw_title="Senior Electronics Engineer"))
    assert band(seniority_level="Entry Level", raw_title="Sr. Software Engineer") == BAND_STRONG


def test_high_compensation_can_supply_seniority_when_both_signals_miss() -> None:
    assert band(seniority_level="Mid Level", raw_title="Software Engineer") == BAND_STRONG


def test_a_posting_with_no_seniority_signal_at_all_is_possible() -> None:
    assert (
        band(seniority_level=None, raw_title="Software Engineer", comp_max=None, comp_min=None)
        == BAND_POSSIBLE
    )


# ----- demotions ----------------------------------------------------------


def test_role_shape_titles_demote() -> None:
    for title, reason in (
        ("Solutions Engineer", "customer-facing title"),
        ("Data Analyst", "analyst/research title"),
        ("RF Engineer", "hardware/physical title"),
        ("Desktop Engineer", "network/IT ops title"),
    ):
        verdict = assign_band(posting(title=title), SETTINGS)
        assert verdict.band == BAND_POSSIBLE, title
        assert reason in verdict.demote_reasons


def test_an_engineering_token_suppresses_the_analyst_rule() -> None:
    """ "AI Engineer/Scientist" is an engineering role containing a research word."""
    assert "analyst/research title" not in demote_reasons(
        posting(title="AI Engineer/Scientist"), SETTINGS
    )


def test_governance_tooling_demotes_only_without_build_tooling() -> None:
    assert "compliance/GRC tooling" in demote_reasons(
        posting(tools=("FedRAMP", "NIST 800-53", "Python")), SETTINGS
    )
    assert "compliance/GRC tooling" not in demote_reasons(
        posting(tools=("FedRAMP", "Terraform", "Kubernetes")), SETTINGS
    )


def test_it_admin_tooling_demotes_only_without_build_tooling() -> None:
    assert "IT-admin tooling" in demote_reasons(
        posting(tools=("Microsoft 365", "Entra ID", "Python")), SETTINGS
    )
    assert "IT-admin tooling" not in demote_reasons(
        posting(tools=("Active Directory", "Kubernetes", "Terraform")), SETTINGS
    )


def test_no_backend_tooling_means_not_strong() -> None:
    assert band(tools=("Microsoft Excel",)) == BAND_POSSIBLE


# ----- reason recording ---------------------------------------------------


def test_every_applicable_reject_reason_is_recorded() -> None:
    """Stopping at the first match makes the discards unauditable."""
    reasons = reject_reasons(posting(is_expired=True, source="usagov", comp_max=90_000), SETTINGS)

    assert {"expired", "public-sector source", "comp below floor"} <= set(reasons)


def test_a_comp_only_rejection_is_distinguishable() -> None:
    """This is the set to re-read whenever the floor moves."""
    verdict = assign_band(posting(comp_max=170_000, comp_min=160_000), SETTINGS)

    assert verdict.reject_reasons == ("comp below floor",)


def test_demote_reasons_are_empty_on_a_rejected_posting() -> None:
    """Rejection is terminal; mixing the two would muddle the counts."""
    verdict = assign_band(posting(is_expired=True), SETTINGS)

    assert verdict.band == BAND_REJECTED
    assert verdict.demote_reasons == ()


# ----- stage output -------------------------------------------------------


def test_run_writes_every_band_and_a_meta_sidecar(tmp_path: Path) -> None:
    records = [
        posting(object_id="s").as_record(),
        posting(object_id="p", title="Data Analyst").as_record(),
        posting(object_id="r", is_expired=True).as_record(),
    ]
    result = run_screen(records, tmp_path, SETTINGS, compress=False)

    written = list(read_jsonl(result.screened_path))
    assert len(written) == 3
    assert {row["band"] for row in written} == {BAND_STRONG, BAND_POSSIBLE, BAND_REJECTED}

    meta: Any = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert meta["bands"][BAND_STRONG] == 1
    assert meta["settings"]["comp_floor"] == SETTINGS.comp_floor


def test_output_is_ordered_strong_first(tmp_path: Path) -> None:
    records = [
        posting(object_id="r", is_expired=True).as_record(),
        posting(object_id="s").as_record(),
    ]
    result = run_screen(records, tmp_path, SETTINGS, compress=False)

    assert [row["band"] for row in read_jsonl(result.screened_path)] == [
        BAND_STRONG,
        BAND_REJECTED,
    ]


def test_near_misses_are_counted(tmp_path: Path) -> None:
    records = [
        posting(object_id="n", comp_max=170_000, comp_min=160_000).as_record(),
        posting(object_id="s").as_record(),
    ]
    result = run_screen(records, tmp_path, SETTINGS, compress=False)

    assert result.near_misses == 1


def test_rejected_postings_are_written_not_dropped(tmp_path: Path) -> None:
    """A filter whose discards are invisible cannot be checked."""
    records = [posting(object_id=str(n), is_expired=True).as_record() for n in range(5)]
    result = run_screen(records, tmp_path, SETTINGS, compress=False)

    assert len(list(read_jsonl(result.screened_path))) == 5
    assert result.counts[BAND_REJECTED] == 5
