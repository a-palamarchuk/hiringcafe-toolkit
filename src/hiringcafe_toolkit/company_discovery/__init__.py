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

__all__ = [
    "CareersLink",
    "DerivationTier",
    "RollupOptions",
    "RollupResult",
    "ScrapeResult",
    "derive_careers_link",
    "rollup_records",
    "run_rollup",
    "run_scrape",
]
