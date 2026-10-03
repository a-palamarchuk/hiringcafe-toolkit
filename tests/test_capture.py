"""Tests for importing browser-captured result pages.

The capture path exists for days the live client is blocked, so its failure
modes are the ones that would make a partial import look complete: a missing
page, a missing search, a near-identical search taken for the configured one,
and stale pages from an earlier session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from typer.testing import CliRunner

from hiringcafe_toolkit.api import BlockedError, search_url
from hiringcafe_toolkit.cli import app
from hiringcafe_toolkit.common.capture import (
    CAPTURE_FORMAT,
    CaptureError,
    CapturePageSource,
    load_capture,
    match_captures,
)
from hiringcafe_toolkit.common.jsonl import read_jsonl
from hiringcafe_toolkit.common.scrape import Search, run_scrape

JsonDict = dict[str, Any]

LOCAL: JsonDict = {"locations": [{"id": "us-zip-20124"}], "dateFetchedPastNDays": 21}
REMOTE: JsonDict = {"workplaceTypes": ["Remote"], "dateFetchedPastNDays": 21}


def entry(
    state: JsonDict,
    number: int,
    *ids: str,
    captured_at: str = "2026-10-02T10:00:00Z",
    total: int | None = None,
) -> JsonDict:
    props: JsonDict = {"ssrHits": [{"objectID": i} for i in ids]}
    if total is not None:
        props["ssrTotalCount"] = total
    return {
        "url": "https://hiringcafe.com/classic",
        "searchState": state,
        "page": number,
        "source": "ssr" if number == 0 else "data",
        "build_id": "build-1",
        "captured_at": captured_at,
        "payload": {"pageProps": props},
    }


def write_capture(path: Path, *pages: JsonDict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"format": CAPTURE_FORMAT, "saved_at": "2026-10-02T10:30:00Z", "pages": list(pages)}
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# ----- loading ------------------------------------------------------------


def test_pages_are_grouped_by_search_and_ordered(tmp_path: Path) -> None:
    path = write_capture(
        tmp_path / "capture-1.json",
        entry(REMOTE, 1, "c"),
        entry(LOCAL, 0, "a"),
        entry(REMOTE, 0, "b"),
    )
    captures = {json.dumps(c.search_state, sort_keys=True): c for c in load_capture(path)}

    remote = captures[json.dumps(REMOTE, sort_keys=True)]
    assert remote.page_numbers == [0, 1]
    assert [r["objectID"] for p in remote.pages for r in p.records] == ["b", "c"]


def test_search_state_key_order_does_not_split_a_search(tmp_path: Path) -> None:
    reordered = {"dateFetchedPastNDays": 21, "workplaceTypes": ["Remote"]}
    path = write_capture(tmp_path / "c.json", entry(REMOTE, 0, "a"), entry(reordered, 1, "b"))

    [capture] = load_capture(path)
    assert capture.page_numbers == [0, 1]


def test_a_recaptured_page_keeps_the_latest_copy(tmp_path: Path) -> None:
    path = write_capture(
        tmp_path / "c.json",
        entry(REMOTE, 0, "new", captured_at="2026-10-02T11:00:00Z"),
        entry(REMOTE, 0, "old", captured_at="2026-10-02T10:00:00Z"),
    )
    [capture] = load_capture(path)

    assert [r["objectID"] for r in capture.pages[0].records] == ["new"]


def test_gaps_and_the_last_page_are_reported(tmp_path: Path) -> None:
    path = write_capture(
        tmp_path / "c.json",
        entry(REMOTE, 0, "a", "b"),
        entry(REMOTE, 1, "c", "d"),
        entry(REMOTE, 3, "e"),
    )
    [capture] = load_capture(path)

    assert capture.missing_pages == [2]
    assert capture.last_page_records == 1


def test_a_long_capture_span_is_measured(tmp_path: Path) -> None:
    path = write_capture(
        tmp_path / "c.json",
        entry(REMOTE, 0, "a", captured_at="2026-10-01T10:00:00Z"),
        entry(REMOTE, 1, "b", captured_at="2026-10-02T10:00:00Z"),
    )
    [capture] = load_capture(path)

    assert capture.capture_span_hours == pytest.approx(24.0)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("not json", "invalid JSON"),
        (json.dumps({"format": "other", "pages": []}), "not a hiringcafe-capture/1"),
        (json.dumps({"format": CAPTURE_FORMAT, "pages": {}}), "pages must be a list"),
        (
            json.dumps({"format": CAPTURE_FORMAT, "pages": [{**entry(REMOTE, 0), "page": -1}]}),
            "valid page number",
        ),
        (
            json.dumps({"format": CAPTURE_FORMAT, "pages": [{**entry(REMOTE, 0), "payload": {}}]}),
            "no pageProps",
        ),
        (
            json.dumps(
                {
                    "format": CAPTURE_FORMAT,
                    "pages": [
                        {**entry(REMOTE, 1), "payload": {"pageProps": {"__N_REDIRECT": "/x"}}}
                    ],
                }
            ),
            "redirect",
        ),
    ],
)
def test_malformed_captures_are_refused(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "c.json"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(CaptureError, match=message):
        load_capture(path)


# ----- matching and serving -----------------------------------------------


def test_a_capture_may_differ_from_its_search_only_in_the_window(tmp_path: Path) -> None:
    daily = {**REMOTE, "dateFetchedPastNDays": 2}
    other = {**REMOTE, "workplaceTypes": ["Hybrid"]}
    path = write_capture(tmp_path / "c.json", entry(daily, 0, "a"), entry(other, 0, "b"))

    matched, unmatched = match_captures([LOCAL, REMOTE], load_capture(path))

    assert matched[0] is None
    assert matched[1] is not None and matched[1].window_days == 2
    assert [c.search_state for c in unmatched] == [other]


def test_the_wider_window_wins_when_a_search_was_captured_twice(tmp_path: Path) -> None:
    daily = {**REMOTE, "dateFetchedPastNDays": 2}
    path = write_capture(tmp_path / "c.json", entry(daily, 0, "a"), entry(REMOTE, 0, "b"))

    matched, unmatched = match_captures([REMOTE], load_capture(path))

    assert matched[0] is not None and matched[0].window_days == 21
    assert [c.window_days for c in unmatched] == [2]


def test_a_daily_capture_is_served_for_its_configured_search(tmp_path: Path) -> None:
    daily = {**REMOTE, "dateFetchedPastNDays": 2}
    path = write_capture(tmp_path / "c.json", entry(daily, 0, "a"), entry(daily, 1, "b"))
    matched, _ = match_captures([REMOTE], load_capture(path))
    source = CapturePageSource([c for c in matched if c is not None])

    result = run_scrape(
        [Search("remote", REMOTE)], tmp_path / "raw", client=source, max_pages=5, compress=False
    )

    assert [r["objectID"] for r in read_jsonl(result.jobs_path)] == ["a", "b"]


# ----- end of results ------------------------------------------------------


def write_capture_with_end(path: Path, end_page: int, *pages: JsonDict) -> Path:
    write_capture(path, *pages)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["ends"] = [{"searchState": REMOTE, "page": end_page, "reason": "no next page link"}]
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_a_capture_reaching_the_recorded_end_is_complete(tmp_path: Path) -> None:
    path = write_capture_with_end(
        tmp_path / "c.json", 1, entry(REMOTE, 0, "a"), entry(REMOTE, 1, "b")
    )
    [capture] = load_capture(path)

    assert capture.complete
    assert capture.summary()["end"] == {"page": 1, "reason": "no next page link"}


def test_an_end_below_the_last_captured_page_is_not_trusted(tmp_path: Path) -> None:
    """An end recorded on page 0 with pages beyond it is a bad record, not an end."""
    path = write_capture_with_end(
        tmp_path / "c.json", 0, entry(REMOTE, 0, "a"), entry(REMOTE, 1, "b")
    )
    [capture] = load_capture(path)

    assert not capture.complete


def test_a_capture_without_an_end_is_incomplete(tmp_path: Path) -> None:
    [capture] = load_capture(
        write_capture(tmp_path / "c.json", entry(REMOTE, 0, "a"), entry(REMOTE, 1, "b"))
    )

    assert not capture.complete


def test_an_empty_last_page_is_an_end_too(tmp_path: Path) -> None:
    """The live scraper's own end signal, in case the link check missed it."""
    [capture] = load_capture(
        write_capture(tmp_path / "c.json", entry(REMOTE, 0, "a"), entry(REMOTE, 1))
    )

    assert capture.complete


def test_captured_pages_scrape_like_live_ones(tmp_path: Path) -> None:
    path = write_capture(
        tmp_path / "c.json",
        entry(LOCAL, 0, "a", "b", total=9),
        entry(LOCAL, 1, "c"),
        entry(REMOTE, 0, "b", "d"),
    )
    source = CapturePageSource(load_capture(path))
    result = run_scrape(
        [Search("local", LOCAL), Search("remote", REMOTE)],
        tmp_path / "raw",
        client=source,
        max_pages=500,
        compress=False,
    )

    assert [r["objectID"] for r in read_jsonl(result.jobs_path)] == ["a", "b", "c", "d"]
    assert [s.stop_reason for s in result.searches] == ["no more pages", "no more pages"]
    assert result.searches[0].reported_totals == {"ssrTotalCount": 9}
    assert result.searches[1].already_written == 1


# ----- search links -------------------------------------------------------


def test_search_url_round_trips_the_search_state() -> None:
    url = urlparse(search_url(REMOTE))

    assert (url.netloc, url.path) == ("hiringcafe.com", "/classic")
    assert json.loads(parse_qs(url.query)["searchState"][0]) == REMOTE


# ----- CLI ----------------------------------------------------------------


def write_config(tmp_path: Path) -> Path:
    states = tmp_path / "config" / "searchstates"
    states.mkdir(parents=True)
    (states / "local.json").write_text(json.dumps(LOCAL), encoding="utf-8")
    (states / "remote.json").write_text(json.dumps(REMOTE), encoding="utf-8")
    config = tmp_path / "config" / "job_shortlist.toml"
    config.write_text(
        '[search]\nsearchstate_paths = ["searchstates/local.json", "searchstates/remote.json"]\n'
        '[capture]\ndir = "../captures"\n',
        encoding="utf-8",
    )
    return config


def invoke(*args: str) -> Any:
    return CliRunner().invoke(app, ["job-shortlist", *args])


def test_import_writes_a_raw_run_from_the_newest_capture(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    write_capture(tmp_path / "captures" / "capture-1.json", entry(LOCAL, 0, "old"))
    write_capture(
        tmp_path / "captures" / "capture-2.json", entry(LOCAL, 0, "a"), entry(REMOTE, 0, "b")
    )
    # Newest by modification time, as with every other stage's default input.
    os.utime(tmp_path / "captures" / "capture-1.json", (1_000, 1_000))
    newest = tmp_path / "captures" / "capture-2.json"
    os.utime(newest, (2_000, 2_000))
    out = tmp_path / "raw"

    result = invoke("import", "--config", str(config), "--out-dir", str(out))

    assert result.exit_code == 0, result.output
    [jobs] = out.glob("jobs-*.jsonl.gz")
    assert [r["objectID"] for r in read_jsonl(jobs)] == ["a", "b"]
    [meta_path] = out.glob("meta-*.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["source"] == "browser capture"
    assert meta["stop_reason"] == "local: no more pages; remote: no more pages"
    assert "page ceiling" not in result.output
    assert meta["capture_file"] == str(newest)
    assert meta["captures"]["remote"]["missing_pages"] == []


def test_import_refuses_when_a_configured_search_was_not_captured(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    capture = write_capture(tmp_path / "c.json", entry(LOCAL, 0, "a"))
    out = tmp_path / "raw"

    result = invoke("import", str(capture), "--config", str(config), "--out-dir", str(out))

    assert result.exit_code == 2
    assert "No captured pages for remote" in result.output
    assert not out.exists()


def test_import_skips_other_searches_and_warns_about_gaps(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    capture = write_capture(
        tmp_path / "c.json",
        entry(LOCAL, 0, "a"),
        entry(REMOTE, 0, "b"),
        entry(REMOTE, 2, "c"),
        entry({"securityClearances": ["Public Trust"]}, 0, "z"),
    )
    out = tmp_path / "raw"

    result = invoke(
        "import", str(capture), "--config", str(config), "--out-dir", str(out), "--no-compress"
    )

    assert result.exit_code == 0, result.output
    assert "Skipping 1 captured pages of a search that is not configured" in result.output
    assert "Missing pages [1]" in result.output
    [jobs] = out.glob("jobs-*.jsonl")
    assert [r["objectID"] for r in read_jsonl(jobs)] == ["a", "b", "c"]


def test_urls_prints_daily_and_catch_up_links(tmp_path: Path) -> None:
    config = write_config(tmp_path)

    result = invoke("urls", "--config", str(config))

    assert result.exit_code == 0, result.output
    daily, catch_up = result.output.split("Catch-up")
    assert "Daily" in daily
    for state in (LOCAL, REMOTE):
        assert search_url({**state, "dateFetchedPastNDays": 2}) in daily
        assert search_url(state) in catch_up


def test_import_warns_about_an_incomplete_search(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    capture = write_capture_with_end(
        tmp_path / "c.json", 1, entry(LOCAL, 0, "a"), entry(REMOTE, 0, "b")
    )

    result = invoke("import", str(capture), "--config", str(config), "--out-dir", str(tmp_path))

    assert result.exit_code == 0, result.output
    # LOCAL has no recorded end; REMOTE's end is page 1, which was not captured.
    assert result.output.count("Last page not reached") == 2


def write_previous_meta(raw: Path, finished_at: str, **fields: Any) -> None:
    raw.mkdir(parents=True, exist_ok=True)
    meta = {"finished_at": finished_at, "unique_records": 10, "stop_reason": "empty page"}
    (raw / f"meta-{finished_at[:10]}.json").write_text(
        json.dumps({**meta, **fields}), encoding="utf-8"
    )


def daily_capture(tmp_path: Path, captured_at: str) -> Path:
    local, remote = ({**s, "dateFetchedPastNDays": 2} for s in (LOCAL, REMOTE))
    return write_capture(
        tmp_path / "c.json",
        entry(local, 0, "a", captured_at=captured_at),
        entry(remote, 0, "b", captured_at=captured_at),
    )


def test_import_warns_when_a_daily_capture_misses_days(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    raw = tmp_path / "raw"
    write_previous_meta(raw, "2026-09-28T10:00:00+00:00")
    capture = daily_capture(tmp_path, "2026-10-02T10:00:00Z")

    result = invoke("import", str(capture), "--config", str(config), "--out-dir", str(raw))

    assert result.exit_code == 0, result.output
    assert "longer than its 2-day window" in result.output


def test_import_does_not_warn_after_yesterdays_run(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    raw = tmp_path / "raw"
    write_previous_meta(raw, "2026-10-01T10:00:00+00:00")
    capture = daily_capture(tmp_path, "2026-10-02T10:00:00Z")

    result = invoke("import", str(capture), "--config", str(config), "--out-dir", str(raw))

    assert result.exit_code == 0, result.output
    assert "may be missed" not in result.output


def test_a_failed_run_is_not_the_previous_run(tmp_path: Path) -> None:
    """A blocked scrape leaves a meta; the daily capture follows the run before it."""
    config = write_config(tmp_path)
    raw = tmp_path / "raw"
    write_previous_meta(raw, "2026-09-28T10:00:00+00:00")
    write_previous_meta(raw, "2026-10-01T10:00:00+00:00", unique_records=0, error="blocked")
    os.utime(raw / "meta-2026-09-28.json", (1_000, 1_000))
    os.utime(raw / "meta-2026-10-01.json", (2_000, 2_000))
    capture = daily_capture(tmp_path, "2026-10-02T10:00:00Z")

    result = invoke("import", str(capture), "--config", str(config), "--out-dir", str(raw))

    assert "may be missed" in result.output


def test_scrape_prints_the_capture_routine_when_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BlockedClient:
        def __init__(self, **_: Any) -> None:
            pass

        def __enter__(self) -> BlockedClient:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def iter_pages(self, search_state: Any, max_pages: int) -> Any:
            raise BlockedError("landing page returned a Cloudflare challenge (HTTP 403)")
            yield  # pragma: no cover - makes this a generator

    monkeypatch.setattr("hiringcafe_toolkit.cli.HiringCafeClient", BlockedClient)
    config = write_config(tmp_path)
    raw = tmp_path / "raw"

    result = invoke("scrape", "--config", str(config), "--out-dir", str(raw))

    assert result.exit_code == 3
    assert "make shortlist-import" in result.output
    assert list(raw.glob("jobs-*")) == []
    assert len(list(raw.glob("meta-*.json"))) == 1
