"""Tests for job-shortlist stage 5.

Weighted toward the queue markup. If the anchor attributes are wrong the page
still looks fine, the extension silently queues nothing or records against the
wrong key, and the `opened` and `applied` labels - which can only be collected
going forward - are lost without any sign.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.job_shortlist.render import (
    render_html,
    run_render,
    sort_postings,
)
from hiringcafe_toolkit.job_shortlist.screen import BAND_POSSIBLE, BAND_STRONG


def row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "object_id": "grnhse___acme___123",
        "band": BAND_STRONG,
        "title": "Software Engineer",
        "raw_title": "Senior Software Engineer",
        "company": "Acme",
        "company_host": "acme.com",
        "comp_min": 190_000,
        "comp_max": 220_000,
        "comp_frequency": "Yearly",
        "seniority_level": "Senior Level",
        "min_years_experience": 8,
        "workplace_type": "Hybrid",
        "cities": ["Reston, Virginia, US"],
        "city_count": 1,
        "tools": ["Java", "Spring Boot", "Kubernetes"],
        "published_at": "2026-08-20T00:00:00Z",
        "source": "grnhse",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/123",
        "alternate_apply_urls": [],
        "demote_reasons": [],
    }
    return {**base, **overrides}


def anchors(markup: str) -> list[str]:
    return re.findall(r"<a [^>]*>", markup)


def queued(markup: str) -> list[str]:
    return [a for a in anchors(markup) if "data-visit-open" in a]


# ----- queue markup -------------------------------------------------------


def test_each_row_queues_the_company_page_then_the_apply_link() -> None:
    """The queue walks the DOM in order, so column order sets the open order."""
    links = queued(render_html([row()], "t"))

    assert len(links) == 2
    assert "acme.com" in links[0]
    assert "https://boards.greenhouse.io/acme/jobs/123" in links[1]


def test_the_company_link_is_keyed_by_host_not_by_posting() -> None:
    """Same key company discovery uses, so processing counts for both pipelines."""
    company = queued(render_html([row()], "t"))[0]

    assert 'data-visit-key="acme.com"' in company
    assert 'data-visit-mark="auto"' in company


def test_an_ats_careers_page_is_replaced_by_the_company_homepage() -> None:
    """One ATS board is only what was posted through that instance."""
    company = queued(render_html([row()], "t"))[0]

    assert 'href="https://acme.com"' in company
    assert "greenhouse" not in company


def test_a_careers_page_on_the_company_domain_is_used_as_is() -> None:
    markup = render_html(
        [row(company_host="dish.com", source="unknown-ats", apply_url="https://jobs.dish.com/x/1")],
        "t",
    )

    assert "jobs.dish.com" in queued(markup)[0]


def test_a_company_without_a_known_host_gets_no_company_link() -> None:
    """The only candidate left is an ATS board, which the extension will not record."""
    links = queued(render_html([row(company_host="")], "t"))

    assert len(links) == 1
    assert "greenhouse" in links[0]


def test_the_queue_key_is_the_prefixed_posting_id() -> None:
    """The prefix keeps the two pipelines apart in a shared export."""
    markup = queued(render_html([row()], "t"))[1]

    assert 'data-visit-key="job:grnhse___acme___123"' in markup


def test_the_apply_link_is_auto_marked() -> None:
    """Marking is the only source of the opened and applied labels."""
    assert 'data-visit-mark="auto"' in queued(render_html([row()], "t"))[1]


def test_a_posting_without_an_id_is_queued_but_not_marked() -> None:
    """Better to reopen it than to record a visit against an empty key."""
    markup = queued(render_html([row(object_id="")], "t"))[1]

    assert "data-visit-open" in markup
    assert "data-visit-key" not in markup
    assert "data-visit-mark" not in markup


def test_an_alternate_apply_url_is_used_when_the_primary_is_missing() -> None:
    markup = render_html([row(apply_url="", alternate_apply_urls=["https://alt.test/job"])], "t")

    assert "https://alt.test/job" in queued(markup)[1]


def test_a_posting_with_no_apply_url_gets_no_queued_link() -> None:
    """A dead link in the queue would open a broken tab and mark it visited."""
    markup = render_html([row(apply_url="", alternate_apply_urls=[])], "t")

    assert len(queued(markup)) == 1  # the company page, no apply link


def test_the_find_column_is_gone() -> None:
    markup = render_html([row()], "t")

    assert "duckduckgo" not in markup
    assert ">find<" not in markup


# ----- ordering -----------------------------------------------------------


def test_strong_sorts_before_possible() -> None:
    ordered = sort_postings(
        [row(band=BAND_POSSIBLE, comp_max=500_000), row(band=BAND_STRONG, comp_max=100_000)]
    )

    assert [p["band"] for p in ordered] == [BAND_STRONG, BAND_POSSIBLE]


def test_higher_compensation_sorts_first_within_a_band() -> None:
    ordered = sort_postings([row(comp_max=180_000), row(comp_max=250_000)])

    assert [p["comp_max"] for p in ordered] == [250_000, 180_000]


def test_unstated_compensation_sorts_last_within_its_band() -> None:
    """An unknown is worth reading, but not ahead of a known number."""
    ordered = sort_postings([row(comp_max=None), row(comp_max=180_000)])

    assert [p["comp_max"] for p in ordered] == [180_000, None]


# ----- cells --------------------------------------------------------------


def test_the_raw_title_is_shown_not_the_normalized_one() -> None:
    """core_job_title has the level stripped, which is what is being scanned for."""
    markup = render_html([row(title="Engineer", raw_title="Principal Engineer")], "t")

    assert "Principal Engineer" in markup


def test_a_compensation_range_is_shown_when_both_ends_are_known() -> None:
    assert "$190,000 - $220,000" in render_html([row()], "t")


def test_unstated_compensation_says_so() -> None:
    assert "not stated" in render_html([row(comp_min=None, comp_max=None)], "t")


def test_an_hourly_derived_figure_is_flagged() -> None:
    """It is annualized on an assumed 2,080 hours, not a number anyone wrote."""
    assert "hourly" in render_html([row(comp_frequency="Hourly")], "t")


def test_a_wide_geography_is_flagged_on_the_compensation_cell() -> None:
    """The ceiling belongs to the priciest metro rather than to this one."""
    markup = render_html([row(city_count=42)], "t")

    assert "42 metros" in markup


def test_demote_reasons_are_shown() -> None:
    """Why a posting is only possible is the judgement being made when skimming."""
    markup = render_html([row(band=BAND_POSSIBLE, demote_reasons=["IT-admin tooling"])], "t")

    assert "IT-admin tooling" in markup


def test_alternate_apply_urls_are_counted_on_the_link() -> None:
    markup = render_html([row(alternate_apply_urls=["https://a.test", "https://b.test"])], "t")

    assert "+2" in markup


def test_extra_tools_are_counted_rather_than_listed() -> None:
    markup = render_html([row(tools=[f"tool{n}" for n in range(9)])], "t")

    assert "+4" in markup


def test_a_company_already_applied_to_is_marked() -> None:
    """Shown rather than filtered: two distinct roles can both be worth applying to."""
    markup = render_html([row()], "t", applied_hosts=frozenset({"acme.com"}))

    assert "resume sent" in markup


def test_an_unapplied_company_is_not_marked() -> None:
    assert "resume sent" not in render_html([row()], "t", applied_hosts=frozenset({"other.com"}))


def test_markup_is_escaped() -> None:
    markup = render_html([row(company='Ac<script> & "Co"')], "t")

    assert "<script>" not in markup
    assert "Ac&lt;script&gt; &amp; &quot;Co&quot;" in markup


def test_attribute_values_are_escaped() -> None:
    """A quote in an id would otherwise break out of the data-visit-key attribute."""
    markup = queued(render_html([row(object_id='a" onload="x')], "t"))[1]

    assert 'onload="x"' not in markup
    assert "&quot;" in markup


# ----- output -------------------------------------------------------------


def test_run_render_writes_a_page(tmp_path: Path) -> None:
    out = tmp_path / "shortlist.html"
    result = run_render([row()], out, title="Job shortlist")

    assert result.rows == 1
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_band_filtering_selects_a_subset(tmp_path: Path) -> None:
    out = tmp_path / "strong.html"
    result = run_render(
        [row(band=BAND_STRONG), row(band=BAND_POSSIBLE)],
        out,
        title="t",
        bands=[BAND_STRONG],
    )

    assert result.rows == 1
    assert result.bands == {BAND_STRONG: 1, BAND_POSSIBLE: 0}


def test_an_empty_shortlist_still_renders_a_page(tmp_path: Path) -> None:
    """A day with nothing new is a normal outcome, not an error."""
    out = tmp_path / "empty.html"
    result = run_render([], out, title="t")

    assert result.rows == 0
    assert "<tbody>" in out.read_text(encoding="utf-8")


def test_the_applied_marker_matches_a_careers_subdomain() -> None:
    """An exact-host check silently misses careers.<company> and reads as 'not applied'."""
    markup = render_html([row()], "t", applied_hosts=frozenset({"careers.acme.com"}))

    assert "resume sent" in markup


def test_the_applied_marker_ignores_a_lookalike_domain() -> None:
    markup = render_html([row()], "t", applied_hosts=frozenset({"notacme.com"}))

    assert "resume sent" not in markup
