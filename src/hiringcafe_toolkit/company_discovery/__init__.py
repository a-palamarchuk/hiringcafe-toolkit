"""Company-discovery pipeline: Scrape -> Rollup -> Filter -> Render.

The scrape stage itself is not here: it is identical for every pipeline and
lives in ``common.scrape``.

Discovers companies with nearby engineering openings and produces a list of
career pages to visit, rather than matching individual job postings.
"""

from hiringcafe_toolkit.company_discovery.render import (
    RenderResult,
    render_html,
    run_render,
    sort_companies,
)
from hiringcafe_toolkit.company_discovery.rollup import (
    RollupOptions,
    RollupResult,
    rollup_records,
    run_rollup,
)
from hiringcafe_toolkit.company_discovery.visited_filter import (
    FilterResult,
    VisitLog,
    VisitLogError,
    filter_companies,
    load_visit_log,
    run_filter,
)

__all__ = [
    "FilterResult",
    "RenderResult",
    "RollupOptions",
    "RollupResult",
    "VisitLog",
    "VisitLogError",
    "filter_companies",
    "load_visit_log",
    "render_html",
    "run_filter",
    "run_render",
    "sort_companies",
    "rollup_records",
    "run_rollup",
]
