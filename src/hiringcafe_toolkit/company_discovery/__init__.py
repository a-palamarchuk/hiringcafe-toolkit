"""Company-discovery pipeline: Scrape -> Rollup -> Filter -> Render.

Discovers companies with nearby engineering openings and produces a list of
career pages to visit, rather than matching individual job postings.
"""

from hiringcafe_toolkit.company_discovery.careers_link import (
    CareersLink,
    DerivationTier,
    derive_careers_link,
)
from hiringcafe_toolkit.company_discovery.rollup import (
    RollupOptions,
    RollupResult,
    rollup_records,
    run_rollup,
)
from hiringcafe_toolkit.company_discovery.scrape import ScrapeResult, run_scrape
from hiringcafe_toolkit.company_discovery.visited_filter import (
    FilterResult,
    VisitLog,
    VisitLogError,
    filter_companies,
    load_visit_log,
    run_filter,
)

__all__ = [
    "CareersLink",
    "DerivationTier",
    "FilterResult",
    "RollupOptions",
    "RollupResult",
    "ScrapeResult",
    "VisitLog",
    "VisitLogError",
    "derive_careers_link",
    "filter_companies",
    "load_visit_log",
    "run_filter",
    "rollup_records",
    "run_rollup",
    "run_scrape",
]
