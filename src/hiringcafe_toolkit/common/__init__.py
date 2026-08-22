"""Shared helpers: configuration, serialization, scraping, and location logic."""

from hiringcafe_toolkit.common.config import (
    CompanyDiscoveryConfig,
    ConfigError,
    HomeLocation,
    JobShortlistConfig,
    RollupSettings,
    ScrapeSettings,
    load_company_discovery_config,
    load_job_shortlist_config,
    load_search_state,
)
from hiringcafe_toolkit.common.jsonl import (
    JsonlWriter,
    is_compressed,
    read_jsonl,
    with_compression,
)
from hiringcafe_toolkit.common.location import (
    Place,
    build_places,
    haversine_miles,
    normalize_states,
    parse_state,
    select_nearby_places,
)
from hiringcafe_toolkit.common.scrape import ScrapeResult, run_scrape
from hiringcafe_toolkit.common.urls import host_to_url, normalize_host

__all__ = [
    "CompanyDiscoveryConfig",
    "ConfigError",
    "HomeLocation",
    "JobShortlistConfig",
    "JsonlWriter",
    "Place",
    "RollupSettings",
    "ScrapeResult",
    "ScrapeSettings",
    "build_places",
    "haversine_miles",
    "host_to_url",
    "is_compressed",
    "load_company_discovery_config",
    "load_job_shortlist_config",
    "load_search_state",
    "normalize_host",
    "normalize_states",
    "parse_state",
    "read_jsonl",
    "run_scrape",
    "select_nearby_places",
    "with_compression",
]
