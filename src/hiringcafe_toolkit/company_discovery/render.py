"""Stage 4 of company discovery: render the visit list.

The output is an HTML table read by the VisitLogger browser extension, which
opens links continuously while keeping a few tabs at a time. Its queue is
opt-in: when a page contains any ``a[data-visit-open]`` anchor, only those are
queued, so helper links on the same row are inert.

Each row therefore carries exactly one queued link - the careers page - marked
with the company's own website host::

    <a href="https://job-boards.greenhouse.io/acme/"
       data-visit-open data-visit-key="acme.com" data-visit-mark="auto">

The key matters because the careers page often lives on a vendor host shared by
thousands of employers. Keying it to the company's own domain is what lets the
visit log dedupe correctly, and what lets an interrupted pass resume: rows
already opened are skipped on the next run.

Rows without a company website get a queued link with no key and no marking, so
they open on every run and are worked through by eye.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

JsonDict = dict[str, Any]

SEARCH_URL = "https://duckduckgo.com/?q="
MAX_TITLES_SHOWN = 3


@dataclass(frozen=True)
class RenderResult:
    path: Path
    rows: int


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _escape(value: Any) -> str:
    return html.escape(_text(value))


def _distance_key(company: Mapping[str, Any]) -> tuple[int, float]:
    """Sort nearest first, with unknown distances last.

    Unknown stays ``None`` in the data rather than becoming a sentinel, so the
    ordering is expressed here instead of being encoded as a fake distance.
    """
    distance = company.get("distance_miles")
    if isinstance(distance, (int, float)) and not isinstance(distance, bool):
        return (0, float(distance))
    return (1, 0.0)


def sort_companies(companies: Iterable[Mapping[str, Any]]) -> list[JsonDict]:
    return [dict(company) for company in sorted(companies, key=_distance_key)]


def _search_url(company: Mapping[str, Any]) -> str:
    name = _text(company.get("name")) or _text(company.get("website_host"))
    return SEARCH_URL + quote_plus(f"{name} careers")


def _format_distance(company: Mapping[str, Any]) -> str:
    distance = company.get("distance_miles")
    if isinstance(distance, (int, float)) and not isinstance(distance, bool):
        return f"{float(distance):.1f}"
    return "-"


def _format_date(company: Mapping[str, Any]) -> str:
    published = _text(company.get("latest_published_at"))
    return _escape(published[:10]) if published else "-"


def _format_locations(company: Mapping[str, Any]) -> str:
    locations = company.get("locations")
    if not isinstance(locations, list) or not locations:
        return "-"
    # City name only: the state and country repeat on every row and the search
    # is regional, so they add width without adding information.
    labels = []
    for location in locations:
        label = _text(location)
        if label:
            labels.append(label.split(",")[0].strip())
    return _escape(", ".join(labels)) if labels else "-"


def _format_titles(company: Mapping[str, Any]) -> str:
    titles = company.get("sample_titles")
    if not isinstance(titles, list) or not titles:
        return "-"
    shown = [_escape(title) for title in titles[:MAX_TITLES_SHOWN] if _text(title)]
    return "<br>".join(shown) if shown else "-"


def _format_postings(company: Mapping[str, Any]) -> str:
    total = company.get("posting_count")
    software = company.get("software_posting_count")
    total_text = str(total) if isinstance(total, int) else "-"
    if isinstance(software, int) and software:
        return f"{total_text} ({software} sw)"
    return total_text


def _careers_cell(company: Mapping[str, Any]) -> str:
    """The one queued link in the row."""
    careers_url = _text(company.get("careers_url"))
    if not careers_url:
        return "-"
    host = _text(company.get("website_host"))
    attributes = ' data-visit-open data-visit-mark="auto"' if host else " data-visit-open"
    if host:
        attributes += f' data-visit-key="{_escape(host)}"'
    return f'<a href="{_escape(careers_url)}"{attributes}>careers</a>'


def _company_cell(company: Mapping[str, Any]) -> str:
    """Company name, linked to its website. Never queued."""
    name = _escape(company.get("name")) or _escape(company.get("website_host")) or "(unnamed)"
    website = _text(company.get("website_url"))
    if not website:
        return name
    return f'<a href="{_escape(website)}">{name}</a>'


def _row(index: int, company: Mapping[str, Any]) -> str:
    cells = [
        f"<td>{index}</td>",
        f'<td class="num">{_format_distance(company)}</td>',
        f"<td>{_careers_cell(company)}</td>",
        f"<td>{_company_cell(company)}</td>",
        f'<td><a href="{_escape(_search_url(company))}">search</a></td>',
        f"<td>{_format_locations(company)}</td>",
        f'<td class="num">{_format_postings(company)}</td>',
        f'<td class="titles">{_format_titles(company)}</td>',
        f'<td class="num">{_format_date(company)}</td>',
    ]
    return "<tr>" + "".join(cells) + "</tr>"


STYLE = """
    body { font-family: system-ui, sans-serif; margin: 1.5em; }
    h1 { font-size: 1.2em; }
    p.meta { color: #555; font-size: 0.9em; }
    table { border-collapse: collapse; width: 100%; font-size: 0.9em; }
    th, td { border: 1px solid #ccc; padding: 4px 7px; text-align: left;
             vertical-align: top; }
    th { background: #f0f0f0; position: sticky; top: 0; }
    td.num { text-align: right; white-space: nowrap; }
    td.titles { color: #333; }
    tr:nth-child(even) { background: #fafafa; }
"""

HEADERS = (
    "#",
    "Miles",
    "Careers",
    "Company",
    "Find",
    "Locations",
    "Posts",
    "Sample titles",
    "Latest",
)


def render_html(companies: Iterable[Mapping[str, Any]], title: str, note: str = "") -> str:
    """Render companies as a VisitLogger-compatible HTML table."""
    ordered = sort_companies(companies)
    header_cells = "".join(f"<th>{html.escape(header)}</th>" for header in HEADERS)
    rows = "\n".join(_row(index, company) for index, company in enumerate(ordered, start=1))
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    note_html = f'<p class="meta">{html.escape(note)}</p>\n' if note else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<p class="meta">{len(ordered)} companies, nearest first. Generated {generated}.</p>
{note_html}<table>
<thead><tr>{header_cells}</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>
"""


def run_render(
    companies: Iterable[Mapping[str, Any]],
    out_path: Path,
    *,
    title: str,
    note: str = "",
) -> RenderResult:
    """Render companies to an HTML file."""
    ordered = sort_companies(companies)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(ordered, title, note), encoding="utf-8")
    return RenderResult(path=out_path, rows=len(ordered))
