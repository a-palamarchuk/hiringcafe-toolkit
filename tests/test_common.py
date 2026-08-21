"""Tests for configuration loading and JSONL helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiringcafe_toolkit.common.config import (
    ConfigError,
    load_company_discovery_config,
    load_search_state,
)
from hiringcafe_toolkit.common.jsonl import JsonlWriter, read_jsonl

VALID_CONFIG = """
[search]
searchstate_path = "searchstates/local.json"

[scrape]
delay_seconds = 2.5
max_pages = 42

[home]
latitude = 38.9531
longitude = -77.4565

[location]
radius_miles = 25
excluded_states = ["Maryland"]

[filters]
excluded_sources = ["usagov"]
excluded_website_tlds = [".gov"]
"""

MINIMAL_CONFIG = """
[search]
searchstate_path = "s.json"

[home]
latitude = 38.9531
longitude = -77.4565
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_reads_values(tmp_path: Path) -> None:
    config_path = write(tmp_path / "config" / "company_discovery.toml", VALID_CONFIG)
    settings = load_company_discovery_config(config_path)

    assert settings.scrape.delay_seconds == 2.5
    assert settings.scrape.max_pages == 42
    assert settings.home.latitude == 38.9531
    assert settings.home.longitude == -77.4565
    assert settings.rollup.radius_miles == 25.0
    assert settings.rollup.excluded_states == ("Maryland",)
    assert settings.rollup.excluded_sources == ("usagov",)
    assert settings.rollup.excluded_website_tlds == (".gov",)
    # Relative searchState paths resolve against the config file, not the cwd.
    assert settings.searchstate_path == (tmp_path / "config" / "searchstates" / "local.json")


def test_load_config_applies_defaults_for_optional_sections(tmp_path: Path) -> None:
    config_path = write(tmp_path / "c.toml", MINIMAL_CONFIG)
    settings = load_company_discovery_config(config_path)

    assert settings.scrape.delay_seconds == 1.0
    assert settings.scrape.max_pages == 500
    assert settings.rollup.radius_miles == 30.0
    assert settings.rollup.excluded_states == ()
    assert settings.rollup.excluded_sources == ()
    assert settings.rollup.excluded_website_tlds == ()


def test_load_config_keeps_absolute_searchstate_path(tmp_path: Path) -> None:
    absolute = tmp_path / "elsewhere" / "s.json"
    config_path = write(
        tmp_path / "c.toml",
        f'[search]\nsearchstate_path = "{absolute}"\n[home]\nlatitude = 38.7\nlongitude = -77.3\n',
    )

    assert load_company_discovery_config(config_path).searchstate_path == absolute


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[scrape]\nmax_pages = 5\n", "missing required \\[search\\]"),
        ('[search]\nsearchstate_path = ""\n', "non-empty string"),
        ('[search]\nsearchstate_path = "s.json"\n', "missing required \\[home\\]"),
        (MINIMAL_CONFIG + "[scrape]\ndelay_seconds = -1\n", "non-negative"),
        (MINIMAL_CONFIG + "[scrape]\nmax_pages = 0\n", "positive integer"),
        (MINIMAL_CONFIG + "[location]\nradius_miles = -5\n", "must be >= 0"),
        (MINIMAL_CONFIG + '[location]\nexcluded_states = "Maryland"\n', "list of strings"),
        (MINIMAL_CONFIG + "[filters]\nexcluded_sources = [1]\n", "list of strings"),
        ('[search]\nsearchstate_path = "s.json"\n[home]\nlatitude = 100\nlongitude = 0\n', "<= 90"),
        ("[search\n", "invalid TOML"),
    ],
)
def test_load_config_rejects_bad_input(tmp_path: Path, body: str, message: str) -> None:
    config_path = write(tmp_path / "c.toml", body)
    with pytest.raises(ConfigError, match=message):
        load_company_discovery_config(config_path)


def test_load_config_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Copy the matching"):
        load_company_discovery_config(tmp_path / "absent.toml")


def test_load_search_state_round_trips(tmp_path: Path) -> None:
    state = {"commitmentTypes": ["Full Time"], "dateFetchedPastNDays": -1}
    path = write(tmp_path / "s.json", json.dumps(state))

    assert load_search_state(path) == state


@pytest.mark.parametrize(
    ("body", "message"),
    [("{}", "empty"), ("[]", "must be a JSON object"), ("{not json", "invalid JSON")],
)
def test_load_search_state_rejects_bad_input(tmp_path: Path, body: str, message: str) -> None:
    path = write(tmp_path / "s.json", body)
    with pytest.raises(ConfigError, match=message):
        load_search_state(path)


def test_jsonl_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "out.jsonl"
    with JsonlWriter(path) as writer:
        writer.write({"objectID": "a", "text": 'quotes " and unicode é'})
        writer.write({"objectID": "b"})

    assert path.read_text(encoding="utf-8").count("\n") == 2
    assert [r["objectID"] for r in read_jsonl(path)] == ["a", "b"]


def test_read_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    path = write(tmp_path / "j.jsonl", '{"a":1}\n\n{"a":2}\n')
    assert [r["a"] for r in read_jsonl(path)] == [1, 2]


def test_read_jsonl_reports_bad_line_number(tmp_path: Path) -> None:
    path = write(tmp_path / "j.jsonl", '{"a":1}\nnot json\n')
    with pytest.raises(ValueError, match=":2:"):
        list(read_jsonl(path))
