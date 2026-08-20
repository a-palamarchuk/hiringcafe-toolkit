"""Company-discovery pipeline: Scrape -> Rollup -> Filter -> Render.

Discovers companies with nearby engineering openings and produces a list of
career pages to visit, rather than matching individual job postings.
"""

from hiringcafe_toolkit.company_discovery.scrape import ScrapeResult, run_scrape

__all__ = ["ScrapeResult", "run_scrape"]
