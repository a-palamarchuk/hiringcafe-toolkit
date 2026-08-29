"""Stage 5 of job shortlist: render the reading list.

The output is an HTML table read by the VisitLogger browser extension, which
opens links a few tabs at a time and remembers what it opened. Its queue is
opt-in: when a page contains any ``a[data-visit-open]`` anchor, only those are
queued, so the company and search links on the same row stay inert.

Each row carries two queued links, in this order::

    <a href="https://acme.com" data-visit-open
       data-visit-key="acme.com" data-visit-mark="auto">Acme</a>
    <a href="https://boards.greenhouse.io/acme/jobs/123" data-visit-open
       data-visit-key="job:grnhse___acme___123" data-visit-mark="auto">apply</a>

The employer's page first, because an interesting posting is a reason to look
at the company before the role, and the second time that company appears the
extension skips the link on its own - it refuses any key that already carries a
date, so a company with four postings opens once.

The keys differ on purpose. The posting is keyed ``job:<id>`` because apply
URLs sit on ATS domains shared by thousands of employers, and the prefix keeps
the two pipelines apart in a shared export. The employer's page is keyed by
company host, which is the same key company discovery uses - so processing a
company here counts as processing it there. That coupling is deliberate and
one-directional: a visited company never suppresses its postings, since those
are keyed separately.

Keying by posting also makes the queue resumable: rows already opened are
skipped on the next pass, so the backlog drains at whatever pace it is actually
worked.

Marking is what produces the ``opened`` and ``applied`` labels the diff stage
reads back. Those are the only record of which postings were acted on, and
they can only be collected going forward, so the link is marked even though
nothing in the pipeline depends on it.

Both bands go in one page, strong first. The queue walks the DOM in order and
skips marked entries, so a single page works strong-first on its own and keeps
its place without the reader tracking which file they were in.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.common.careers_link import company_entry_url, derive_careers_link
from hiringcafe_toolkit.common.urls import applied_to, posting_key
from hiringcafe_toolkit.job_shortlist.screen import BAND_POSSIBLE, BAND_STRONG

JsonDict = dict[str, Any]

MAX_TOOLS_SHOWN = 5
MAX_CITIES_SHOWN = 2

#: Cities beyond this in one posting mean a geo-tiered pay band, so the stated
#: ceiling belongs to the priciest metro rather than to this one. Flagged in
#: the compensation cell because that is where it would mislead.
WIDE_GEOGRAPHY_CITIES = 5

BAND_ORDER = (BAND_STRONG, BAND_POSSIBLE)


@dataclass(frozen=True)
class RenderResult:
    path: Path
    rows: int
    bands: dict[str, int]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _escape(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _strings(value: Any) -> list[str]:
    return [_text(item) for item in value if _text(item)] if isinstance(value, list) else []


def sort_postings(postings: Iterable[Mapping[str, Any]]) -> list[JsonDict]:
    """Strong first, then by compensation, then by company.

    Postings with no stated compensation sort last within their band rather
    than first: an unknown is worth reading, but not ahead of a known number.
    """
    rank = {band: index for index, band in enumerate(BAND_ORDER)}
    return sorted(
        (dict(posting) for posting in postings),
        key=lambda p: (
            rank.get(_text(p.get("band")), len(rank)),
            -(p.get("comp_max") or 0),
            _text(p.get("company")).lower(),
        ),
    )


def _format_comp(posting: Mapping[str, Any]) -> str:
    """Compensation, with the qualifications that make the number readable."""
    low, high = posting.get("comp_min"), posting.get("comp_max")
    if not isinstance(high, int):
        return '<span class="unknown">not stated</span>'

    text = f"${high:,}"
    if isinstance(low, int) and low != high:
        text = f"${low:,} - ${high:,}"

    flags = []
    if _text(posting.get("comp_frequency")) == "Hourly":
        # Annualized from an hourly rate on an assumed 2,080 hours, so it is a
        # derived figure rather than one the employer wrote.
        flags.append("hourly")
    cities = posting.get("city_count")
    if isinstance(cities, int) and cities >= WIDE_GEOGRAPHY_CITIES:
        flags.append(f"{cities} metros")
    if flags:
        text += f' <span class="flag">({", ".join(flags)})</span>'
    return text


def _format_location(posting: Mapping[str, Any]) -> str:
    cities = _strings(posting.get("cities"))
    workplace = _text(posting.get("workplace_type"))
    shown = ", ".join(city.split(",")[0] for city in cities[:MAX_CITIES_SHOWN])
    if len(cities) > MAX_CITIES_SHOWN:
        shown += f" +{len(cities) - MAX_CITIES_SHOWN}"
    return f'{_escape(shown) or "-"}<br><span class="flag">{_escape(workplace)}</span>'


def _format_seniority(posting: Mapping[str, Any]) -> str:
    """Both seniority signals, since neither is reliable alone.

    The vendor field and the posting's own title disagree about one time in
    seven, in both directions, so showing only one would hide the disagreement
    exactly where it matters.
    """
    field = _text(posting.get("seniority_level")) or "-"
    years = posting.get("min_years_experience")
    suffix = f' <span class="flag">{years}y+</span>' if isinstance(years, int) else ""
    return f"{_escape(field)}{suffix}"


def _format_tools(posting: Mapping[str, Any]) -> str:
    tools = _strings(posting.get("tools"))
    if not tools:
        return '<span class="unknown">none listed</span>'
    shown = ", ".join(tools[:MAX_TOOLS_SHOWN])
    if len(tools) > MAX_TOOLS_SHOWN:
        shown += f" +{len(tools) - MAX_TOOLS_SHOWN}"
    return _escape(shown)


def _format_band(posting: Mapping[str, Any]) -> str:
    """The band, plus why it is not higher."""
    band = _text(posting.get("band"))
    reasons = _strings(posting.get("demote_reasons"))
    cell = f'<span class="band {_escape(band)}">{_escape(band)}</span>'
    if reasons:
        cell += f'<br><span class="flag">{_escape(", ".join(reasons))}</span>'
    return cell


def _apply_cell(posting: Mapping[str, Any]) -> str:
    """The one queued link in the row.

    Marked so the extension records that it was opened, and so F9 can record
    that a resume went in. Postings with no apply URL fall back to the first
    alternate; one with none at all gets no queued link rather than a dead one.
    """
    url = _text(posting.get("apply_url")) or next(
        iter(_strings(posting.get("alternate_apply_urls"))), ""
    )
    if not url:
        return '<span class="unknown">-</span>'

    object_id = _text(posting.get("object_id"))
    attributes = " data-visit-open"
    if object_id:
        attributes += f' data-visit-key="{_escape(posting_key(object_id))}"'
        attributes += ' data-visit-mark="auto"'
    label = "apply"
    alternates = len(_strings(posting.get("alternate_apply_urls")))
    if alternates:
        label += f' <span class="flag">+{alternates}</span>'
    return f'<a href="{_escape(url)}"{attributes}>{label}</a>'


def _company_cell(posting: Mapping[str, Any], applied_hosts: frozenset[str]) -> str:
    """Company name, linked to the page worth opening to process the employer.

    Queued and keyed by company host, so opening it records the company as
    processed for both pipelines. Without a known host there is no link at all:
    the only candidate left would be an ATS board, which the extension refuses
    to record against, so it would open a tab and remember nothing.

    Carries a marker when a resume has already gone to this employer, matched
    by domain rather than exact host - the visit log records whichever host was
    opened, often a careers subdomain like ``careers.appian.com``, while the
    posting carries the registrable domain. Shown rather than filtered: two
    genuinely distinct roles at one company are worth both applications, and
    that is a judgement per row.
    """
    name = _escape(posting.get("company")) or "(unnamed)"
    host = _text(posting.get("company_host"))
    careers = derive_careers_link(_text(posting.get("apply_url")), _text(posting.get("source"))).url
    url = company_entry_url(careers, host)
    if not url:
        return name

    cell = (
        f'<a href="{_escape(url)}" data-visit-open '
        f'data-visit-key="{_escape(host)}" data-visit-mark="auto">{name}</a>'
    )
    if applied_to(host, applied_hosts):
        cell += '<br><span class="applied">resume sent</span>'
    return cell


def _title_cell(posting: Mapping[str, Any]) -> str:
    """The posting's own title, not the normalized one.

    ``core_job_title`` has seniority and level stripped, so "Engineer II" and
    "Senior Engineer" both render as "Engineer" - which is precisely the
    distinction being scanned for.
    """
    return _escape(posting.get("raw_title")) or _escape(posting.get("title")) or "(untitled)"


def _row(index: int, posting: Mapping[str, Any], applied_hosts: frozenset[str]) -> str:
    published = _text(posting.get("published_at"))[:10]
    cells = (
        f'<td class="num">{index}</td>',
        f"<td>{_format_band(posting)}</td>",
        f"<td>{_title_cell(posting)}</td>",
        f"<td>{_company_cell(posting, applied_hosts)}</td>",
        f'<td class="num">{_format_comp(posting)}</td>',
        f"<td>{_format_seniority(posting)}</td>",
        f"<td>{_format_location(posting)}</td>",
        f'<td class="tools">{_format_tools(posting)}</td>',
        f'<td class="num">{_escape(published) or "-"}</td>',
        f'<td class="num">{_apply_cell(posting)}</td>',
    )
    band = _escape(posting.get("band"))
    return f'<tr class="{band}">' + "".join(cells) + "</tr>"


STYLE = """
    body { font-family: system-ui, sans-serif; margin: 1.5em; }
    h1 { font-size: 1.2em; }
    p.meta { color: #555; font-size: 0.9em; }
    table { border-collapse: collapse; width: 100%; font-size: 0.9em; }
    th, td { border: 1px solid #ccc; padding: 4px 7px; text-align: left;
             vertical-align: top; }
    th { background: #f0f0f0; position: sticky; top: 0; }
    td.num { text-align: right; white-space: nowrap; }
    td.tools { color: #333; max-width: 22em; }
    tr:nth-child(even) { background: #fafafa; }
    tr.strong td:first-child { border-left: 3px solid #2e7d32; }
    span.band { font-weight: 600; font-size: 0.85em; }
    span.band.strong { color: #2e7d32; }
    span.band.possible { color: #8a6d00; }
    span.flag { color: #777; font-size: 0.85em; }
    span.unknown { color: #999; }
    span.applied { color: #b25000; font-size: 0.85em; font-weight: 600; }
"""

HEADERS = (
    "#",
    "Band",
    "Title",
    "Company",
    "Compensation",
    "Seniority",
    "Location",
    "Tools",
    "Posted",
    "Apply",
)


def render_html(
    postings: Iterable[Mapping[str, Any]],
    title: str,
    *,
    applied_hosts: frozenset[str] = frozenset(),
    note: str = "",
) -> str:
    """Render postings as a VisitLogger-compatible HTML table."""
    ordered = sort_postings(postings)
    header_cells = "".join(f"<th>{html.escape(header)}</th>" for header in HEADERS)
    rows = "\n".join(
        _row(index, posting, applied_hosts) for index, posting in enumerate(ordered, start=1)
    )
    counts = {band: sum(1 for p in ordered if _text(p.get("band")) == band) for band in BAND_ORDER}
    summary = ", ".join(f"{count} {band}" for band, count in counts.items() if count)
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
<p class="meta">{len(ordered)} postings ({html.escape(summary)}), strongest first. \
Generated {generated}.</p>
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
    postings: Iterable[Mapping[str, Any]],
    out_path: Path,
    *,
    title: str,
    applied_hosts: frozenset[str] = frozenset(),
    bands: Sequence[str] = BAND_ORDER,
    note: str = "",
) -> RenderResult:
    """Render a shortlist to an HTML file."""
    wanted = frozenset(bands)
    ordered = [p for p in sort_postings(postings) if _text(p.get("band")) in wanted]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_html(ordered, title, applied_hosts=applied_hosts, note=note), encoding="utf-8"
    )
    counts = {band: sum(1 for p in ordered if _text(p.get("band")) == band) for band in BAND_ORDER}
    return RenderResult(path=out_path, rows=len(ordered), bands=counts)
