"""Configuration loading.

Scalar settings live in TOML. The search definition itself does not: it is a
deeply nested object copied verbatim out of the hiring.cafe page URL, so it
stays in its own JSON file and TOML only points at it. Hand-translating a
search into TOML would invite silent filter errors, which are expensive here -
a mistyped filter produces a plausible-looking result set that is quietly wrong.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]

DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_MAX_PAGES = 500
DEFAULT_RADIUS_MILES = 30.0
DEFAULT_COMPRESS = True

#: A posting whose stated ceiling falls below this is rejected. Postings that
#: state nothing are never rejected on compensation - they are a large and good
#: slice of the results rather than noise.
DEFAULT_COMP_FLOOR = 180_000

#: A stated band whose bottom falls below this is demoted, not rejected. A wide
#: band means the ceiling belongs to a level or a metro that may not be this
#: one.
DEFAULT_COMP_MIN_FLOOR = 130_000

#: Cities in one posting before its pay band is treated as geo-tiered. Measured:
#: postings spanning five or more cities have a median band spread of 53%
#: against 30% for a single city, so the maximum is the priciest metro's number.
DEFAULT_WIDE_GEOGRAPHY_CITIES = 5


class ConfigError(Exception):
    """The configuration file is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class ScrapeSettings:
    """Politeness and safety limits for a scrape run."""

    delay_seconds: float = DEFAULT_DELAY_SECONDS
    max_pages: int = DEFAULT_MAX_PAGES
    compress: bool = DEFAULT_COMPRESS
    """Gzip the raw records. Roughly 7x on this data, which matters once daily
    runs are retained; the meta sidecar stays plain because it is read by eye."""


@dataclass(frozen=True)
class HomeLocation:
    """The point distances are measured from."""

    latitude: float
    longitude: float


@dataclass(frozen=True)
class RollupSettings:
    """Post-scrape filtering applied when aggregating postings into companies."""

    radius_miles: float = DEFAULT_RADIUS_MILES
    """Re-checked locally: a scraped posting matched on *any* of its locations,
    which may be nowhere near home."""

    excluded_states: tuple[str, ...] = ()
    """A posting survives if any in-radius workplace city is outside these."""

    excluded_sources: tuple[str, ...] = ()
    """ATS sources to drop entirely, e.g. public-sector job boards."""

    excluded_website_tlds: tuple[str, ...] = ()
    """Company website suffixes to drop, e.g. ``.gov``. Source-based exclusion
    misses an agency or university that uses a mainstream ATS; this catches
    them by domain instead."""


@dataclass(frozen=True)
class CompanyDiscoveryConfig:
    """Settings for the company-discovery pipeline."""

    searchstate_path: Path
    scrape: ScrapeSettings
    home: HomeLocation
    rollup: RollupSettings
    visit_log_path: Path | None = None


def _require_table(data: dict[str, Any], key: str, source: Path) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        raise ConfigError(f"{source}: missing required [{key}] section")
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: [{key}] must be a table")
    return value


def _require_number(
    table: dict[str, Any],
    key: str,
    default: float,
    source: Path,
    section: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{source}: [{section}].{key} must be a number")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{source}: [{section}].{key} must be >= {minimum:g}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{source}: [{section}].{key} must be <= {maximum:g}")
    return float(value)


def _string_tuple(table: dict[str, Any], key: str, source: Path, section: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{source}: [{section}].{key} must be a list of strings")
    return tuple(item for item in value if item.strip())


def _require_bool(
    table: dict[str, Any], key: str, default: bool, source: Path, section: str
) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{source}: [{section}].{key} must be true or false")
    return value


def _scrape_settings(data: dict[str, Any], path: Path) -> ScrapeSettings:
    """Parse the [scrape] table, which is identical across pipelines."""
    table = data.get("scrape", {})
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [scrape] must be a table")

    delay = table.get("delay_seconds", DEFAULT_DELAY_SECONDS)
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
        raise ConfigError(f"{path}: [scrape].delay_seconds must be a non-negative number")

    max_pages = table.get("max_pages", DEFAULT_MAX_PAGES)
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
        raise ConfigError(f"{path}: [scrape].max_pages must be a positive integer")

    return ScrapeSettings(
        delay_seconds=float(delay),
        max_pages=max_pages,
        compress=_require_bool(table, "compress", DEFAULT_COMPRESS, path, "scrape"),
    )


def _searchstate_path(data: dict[str, Any], path: Path) -> Path:
    """Resolve [search].searchstate_path, required by every pipeline."""
    search = _require_table(data, "search", path)
    raw = search.get("searchstate_path")
    if not isinstance(raw, str) or not raw:
        raise ConfigError(f"{path}: [search].searchstate_path must be a non-empty string")
    return _resolve(raw, path)


def _resolve(raw: str, config_path: Path) -> Path:
    """Resolve a configured path against the config file.

    Relative paths resolve against the config rather than the working
    directory, so the CLI behaves the same from anywhere.
    """
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return (config_path.parent / candidate).resolve()


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{path}: not found. Copy the matching *.example.toml and fill it in."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc


def load_company_discovery_config(path: Path) -> CompanyDiscoveryConfig:
    """Load and validate ``config/company_discovery.toml``."""
    data = _read_toml(path)

    state_path = _searchstate_path(data, path)
    scrape = _scrape_settings(data, path)

    home_table = _require_table(data, "home", path)
    home = HomeLocation(
        latitude=_require_number(
            home_table, "latitude", 0.0, path, "home", minimum=-90.0, maximum=90.0
        ),
        longitude=_require_number(
            home_table, "longitude", 0.0, path, "home", minimum=-180.0, maximum=180.0
        ),
    )

    location_table = data.get("location", {})
    if not isinstance(location_table, dict):
        raise ConfigError(f"{path}: [location] must be a table")
    filters_table = data.get("filters", {})
    if not isinstance(filters_table, dict):
        raise ConfigError(f"{path}: [filters] must be a table")

    rollup = RollupSettings(
        radius_miles=_require_number(
            location_table, "radius_miles", DEFAULT_RADIUS_MILES, path, "location", minimum=0.0
        ),
        excluded_states=_string_tuple(location_table, "excluded_states", path, "location"),
        excluded_sources=_string_tuple(filters_table, "excluded_sources", path, "filters"),
        excluded_website_tlds=_string_tuple(
            filters_table, "excluded_website_tlds", path, "filters"
        ),
    )

    visit_logger_table = data.get("visit_logger", {})
    if not isinstance(visit_logger_table, dict):
        raise ConfigError(f"{path}: [visit_logger] must be a table")
    raw_visit_log = visit_logger_table.get("export_path")
    if raw_visit_log is not None and not isinstance(raw_visit_log, str):
        raise ConfigError(f"{path}: [visit_logger].export_path must be a string")
    visit_log_path = _resolve(raw_visit_log, path) if raw_visit_log else None

    return CompanyDiscoveryConfig(
        searchstate_path=state_path,
        visit_log_path=visit_log_path,
        scrape=scrape,
        home=home,
        rollup=rollup,
    )


def load_search_state(path: Path) -> JsonDict:
    """Load a searchState object saved from the hiring.cafe page URL."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: searchState file not found") from exc
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"{path}: searchState must be a JSON object")
    if not parsed:
        raise ConfigError(f"{path}: searchState is empty; an unfiltered search is never intended")
    return parsed


@dataclass(frozen=True)
class ScreenSettings:
    """Thresholds and lists for the screen stage.

    Only the values that are personal live here. The word lists that decide
    role shape and discipline stay in code: they encode what the data looks
    like rather than what the user wants, and they need comments to be
    intelligible.
    """

    comp_floor: int = DEFAULT_COMP_FLOOR
    comp_min_floor: int = DEFAULT_COMP_MIN_FLOOR
    wide_geography_cities: int = DEFAULT_WIDE_GEOGRAPHY_CITIES
    company_blocklist: frozenset[str] = frozenset()
    """Lower-cased company names never worth surfacing. Unlike company
    discovery, where a bad company costs one glance, a staffing firm here can
    flood the list every day."""


@dataclass(frozen=True)
class JobShortlistConfig:
    """Settings for the job-shortlist pipeline.

    Deliberately smaller than the company-discovery config. There is no
    ``[home]`` section because this pipeline does no distance work: the sample
    showed every record from a 30-mile search already inside 30 miles, so a
    local radius re-check would drop nothing. Company discovery needs it
    because a posting there can match on a city nowhere near home.

    Screening settings (compensation floor, company blocklist) arrive with the
    screen stage rather than being declared before anything reads them.
    """

    searchstate_path: Path
    scrape: ScrapeSettings
    screen: ScreenSettings = ScreenSettings()


def _positive_int(table: dict[str, Any], key: str, default: int, path: Path) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{path}: [screen].{key} must be a non-negative integer")
    return value


def _screen_settings(data: dict[str, Any], path: Path) -> ScreenSettings:
    table = data.get("screen", {})
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [screen] must be a table")

    raw_blocklist = table.get("company_blocklist", [])
    if not isinstance(raw_blocklist, list) or not all(
        isinstance(name, str) for name in raw_blocklist
    ):
        raise ConfigError(f"{path}: [screen].company_blocklist must be a list of strings")

    settings = ScreenSettings(
        comp_floor=_positive_int(table, "comp_floor", DEFAULT_COMP_FLOOR, path),
        comp_min_floor=_positive_int(table, "comp_min_floor", DEFAULT_COMP_MIN_FLOOR, path),
        wide_geography_cities=_positive_int(
            table, "wide_geography_cities", DEFAULT_WIDE_GEOGRAPHY_CITIES, path
        ),
        # Compared against a lower-cased company name at screening time, so
        # normalize once here rather than at every comparison.
        company_blocklist=frozenset(name.strip().lower() for name in raw_blocklist if name.strip()),
    )
    if settings.comp_min_floor > settings.comp_floor:
        raise ConfigError(
            f"{path}: [screen].comp_min_floor ({settings.comp_min_floor}) exceeds "
            f"comp_floor ({settings.comp_floor}); the band bottom cannot sit above its top"
        )
    return settings


def load_job_shortlist_config(path: Path) -> JobShortlistConfig:
    """Load and validate ``config/job_shortlist.toml``."""
    data = _read_toml(path)
    return JobShortlistConfig(
        searchstate_path=_searchstate_path(data, path),
        scrape=_scrape_settings(data, path),
        screen=_screen_settings(data, path),
    )
