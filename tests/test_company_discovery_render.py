"""Tests for company-discovery stage 4 (render)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.company_discovery.render import (
    render_html,
    run_render,
    sort_companies,
)

JsonDict = dict[str, Any]


def company(**overrides: Any) -> JsonDict:
    base: JsonDict = {
        "name": "Acme",
        "website_host": "acme.com",
        "website_url": "https://acme.com",
        "has_website": True,
        "careers_url": "https://job-boards.greenhouse.io/acme/",
        "careers_tier": "host_and_segments",
        "distance_miles": 10.4,
        "locations": ["Vienna, Virginia, US"],
        "posting_count": 4,
        "software_posting_count": 2,
        "sample_titles": ["Staff Backend Engineer", "SRE"],
        "latest_published_at": "2026-08-15T00:00:00.000Z",
    }
    base.update(overrides)
    return base


def rows_of(html_text: str) -> list[str]:
    return re.findall(r"<tr>(?:(?!</tr>).)*</tr>", html_text, flags=re.S)[1:]


# ----- ordering ----------------------------------------------------------


def test_companies_sort_nearest_first() -> None:
    ordered = sort_companies(
        [company(name="Far", distance_miles=25.0), company(name="Near", distance_miles=3.2)]
    )
    assert [c["name"] for c in ordered] == ["Near", "Far"]


def test_unknown_distance_sorts_last() -> None:
    """Unknown stays None in the data rather than becoming a fake large number."""
    ordered = sort_companies(
        [
            company(name="Unknown", distance_miles=None),
            company(name="Far", distance_miles=29.9),
        ]
    )
    assert [c["name"] for c in ordered] == ["Far", "Unknown"]


def test_rows_are_numbered_after_sorting() -> None:
    page = render_html(
        [company(name="Far", distance_miles=25.0), company(name="Near", distance_miles=3.2)],
        "T",
    )
    first, second = rows_of(page)
    assert "<td>1</td>" in first and "Near" in first
    assert "<td>2</td>" in second and "Far" in second


# ----- VisitLogger contract ---------------------------------------------


def test_the_company_page_is_the_only_queued_link() -> None:
    """An ATS board lists only what was posted through that one instance."""
    page = render_html([company()], "T")
    queued = re.findall(r'<a href="([^"]+)"[^>]*data-visit-open', page)
    assert queued == ["https://acme.com"]


def test_a_careers_page_on_the_company_domain_is_queued_instead() -> None:
    page = render_html(
        [company(website_host="dish.com", careers_url="https://jobs.dish.com/")], "T"
    )
    queued = re.findall(r'<a href="([^"]+)"[^>]*data-visit-open', page)
    assert queued == ["https://jobs.dish.com/"]


def test_the_queued_link_is_keyed_to_the_company_domain() -> None:
    """Also the key the job-shortlist pipeline uses, so processing counts in both."""
    page = render_html([company()], "T")
    assert 'data-visit-key="acme.com"' in page
    assert 'data-visit-mark="auto"' in page


def test_careers_and_search_links_are_not_queued() -> None:
    """The board stays one click away without being opened automatically."""
    page = render_html([company()], "T")
    assert '<a href="https://job-boards.greenhouse.io/acme/">careers</a>' in page
    assert "duckduckgo.com" in page
    assert page.count("data-visit-open") == 1


def test_search_link_uses_the_company_name() -> None:
    page = render_html([company(name="Applied Intuition")], "T")
    assert "q=Applied+Intuition+careers" in page


def test_website_less_row_has_no_queued_link() -> None:
    """The only candidate left is an ATS board, which the extension will not record."""
    page = render_html([company(website_host=None, website_url=None, has_website=False)], "T")
    assert "data-visit-open" not in page


def test_a_row_without_a_careers_url_still_queues_the_homepage() -> None:
    """The employer is worth looking at even when no board URL could be derived."""
    page = render_html([company(careers_url=None)], "T")
    queued = re.findall(r'<a href="([^"]+)"[^>]*data-visit-open', page)
    assert queued == ["https://acme.com"]


# ----- cell formatting ---------------------------------------------------


def test_distance_is_shown_to_one_decimal() -> None:
    assert '<td class="num">10.4</td>' in render_html([company()], "T")


def test_unknown_distance_renders_as_a_dash() -> None:
    page = render_html([company(distance_miles=None)], "T")
    assert '<td class="num">-</td>' in page


def test_posting_count_shows_the_software_share() -> None:
    assert "4 (2 sw)" in render_html([company()], "T")


def test_posting_count_omits_a_zero_software_share() -> None:
    page = render_html([company(posting_count=3, software_posting_count=0)], "T")
    assert "(0 sw)" not in page
    assert '<td class="num">3</td>' in page


def test_locations_are_trimmed_to_city_names() -> None:
    page = render_html([company(locations=["Vienna, Virginia, US", "Reston, Virginia, US"])], "T")
    assert "Vienna, Reston" in page


def test_titles_are_capped_at_three() -> None:
    page = render_html([company(sample_titles=["A", "B", "C", "D"])], "T")
    assert "A<br>B<br>C" in page
    assert ">D<" not in page


def test_date_is_trimmed_to_the_day() -> None:
    assert "2026-08-15" in render_html([company()], "T")
    assert "T00:00:00" not in render_html([company()], "T")


def test_missing_fields_render_as_dashes() -> None:
    page = render_html(
        [{"name": "Bare", "website_host": "bare.com", "careers_url": "https://bare.com/jobs"}],
        "T",
    )
    assert page.count("-</td>") >= 3


def test_unnamed_company_still_renders() -> None:
    page = render_html([company(name=None, website_host=None, website_url=None)], "T")
    assert "(unnamed)" in page


# ----- escaping ----------------------------------------------------------


def test_company_name_is_escaped() -> None:
    page = render_html([company(name="Ben & Jerry's <script>")], "T")
    assert "<script>" not in page
    assert "Ben &amp; Jerry&#x27;s" in page


def test_titles_are_escaped() -> None:
    page = render_html([company(sample_titles=["Engineer <b>III</b>"])], "T")
    assert "<b>III</b>" not in page


def test_urls_are_escaped() -> None:
    page = render_html([company(careers_url='https://x.test/?a=1&b="2"')], "T")
    assert "a=1&amp;b=&quot;2&quot;" in page


# ----- file output -------------------------------------------------------


def test_run_render_writes_the_page(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "companies.html"
    result = run_render([company(), company(name="Beta")], out, title="Visit list", note="Hi")

    text = out.read_text(encoding="utf-8")
    assert result.rows == 2
    assert text.startswith("<!DOCTYPE html>")
    assert "<title>Visit list</title>" in text
    assert "Hi" in text
    assert "2 companies" in text


def test_empty_input_still_renders_a_valid_page(tmp_path: Path) -> None:
    result = run_render([], tmp_path / "empty.html", title="Nothing")
    assert result.rows == 0
    assert "<tbody>" in result.path.read_text(encoding="utf-8")
