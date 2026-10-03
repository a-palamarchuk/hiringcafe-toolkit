"""Client for hiring.cafe's search API, shared across pipelines."""

from hiringcafe_toolkit.api.client import (
    BASE_URL,
    SSR_PAGE_LABEL,
    BlockedError,
    HiringCafeClient,
    HiringCafeError,
    ResponseParseError,
    ResultPage,
    StaleBuildIdError,
    compact_json,
    extract_next_data,
    find_records,
    find_reported_totals,
    record_key,
    search_url,
)

__all__ = [
    "BASE_URL",
    "SSR_PAGE_LABEL",
    "BlockedError",
    "HiringCafeClient",
    "HiringCafeError",
    "ResponseParseError",
    "ResultPage",
    "StaleBuildIdError",
    "compact_json",
    "extract_next_data",
    "find_records",
    "find_reported_totals",
    "record_key",
    "search_url",
]
