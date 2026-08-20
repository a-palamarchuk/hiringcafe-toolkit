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


class ConfigError(Exception):
    """The configuration file is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class ScrapeSettings:
    """Politeness and safety limits for a scrape run."""

    delay_seconds: float = DEFAULT_DELAY_SECONDS
    max_pages: int = DEFAULT_MAX_PAGES


@dataclass(frozen=True)
class CompanyDiscoveryConfig:
    """Settings for the company-discovery pipeline."""

    searchstate_path: Path
    scrape: ScrapeSettings


def _require_table(data: dict[str, Any], key: str, source: Path) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        raise ConfigError(f"{source}: missing required [{key}] section")
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: [{key}] must be a table")
    return value


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

    search = _require_table(data, "search", path)
    raw_state_path = search.get("searchstate_path")
    if not isinstance(raw_state_path, str) or not raw_state_path:
        raise ConfigError(f"{path}: [search].searchstate_path must be a non-empty string")

    # Relative paths resolve against the config file, so a config can be run
    # from any working directory.
    state_path = Path(raw_state_path)
    if not state_path.is_absolute():
        state_path = (path.parent / state_path).resolve()

    scrape_table = data.get("scrape", {})
    if not isinstance(scrape_table, dict):
        raise ConfigError(f"{path}: [scrape] must be a table")

    delay = scrape_table.get("delay_seconds", DEFAULT_DELAY_SECONDS)
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
        raise ConfigError(f"{path}: [scrape].delay_seconds must be a non-negative number")

    max_pages = scrape_table.get("max_pages", DEFAULT_MAX_PAGES)
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
        raise ConfigError(f"{path}: [scrape].max_pages must be a positive integer")

    return CompanyDiscoveryConfig(
        searchstate_path=state_path,
        scrape=ScrapeSettings(delay_seconds=float(delay), max_pages=max_pages),
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
