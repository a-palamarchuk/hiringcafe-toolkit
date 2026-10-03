"""Tests for the shared scrape stage.

The client is stubbed here: transport behavior is covered in
test_api_client.py, so these focus on deduplication, stop conditions, and the
meta sidecar that tells you whether a run actually finished.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from hiringcafe_toolkit.api.client import HiringCafeError, ResultPage
from hiringcafe_toolkit.common.jsonl import read_jsonl
from hiringcafe_toolkit.common.scrape import Search, run_scrape

JsonDict = dict[str, Any]


class StubClient:
    """Yields canned pages; records how many were consumed."""

    def __init__(self, pages: list[ResultPage], error: Exception | None = None) -> None:
        self.pages = pages
        self.error = error
        self.consumed = 0
        self.max_pages_seen: int | None = None

    def iter_pages(self, search_state: Mapping[str, Any], max_pages: int) -> Iterator[ResultPage]:
        self.max_pages_seen = max_pages
        for page in self.pages:
            self.consumed += 1
            yield page
        if self.error is not None:
            raise self.error


def page(label: str, *ids: str, totals: dict[str, float] | None = None) -> ResultPage:
    return ResultPage(
        label=label,
        records=[{"objectID": i, "payload": f"raw-{i}"} for i in ids],
        reported_totals=totals or {},
        build_id="build-1",
    )


def read_meta(path: Path) -> JsonDict:
    loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def run(tmp_path: Path, client: Any, max_pages: int = 50, compress: bool = False) -> Any:
    # Defaults to uncompressed so the existing assertions read plain files;
    # compression itself is covered explicitly below.
    return run_scrape(
        [Search("local", {"q": 1})],
        tmp_path,
        client=client,
        max_pages=max_pages,
        compress=compress,
    )


def test_records_are_written_verbatim_and_deduplicated(tmp_path: Path) -> None:
    client = StubClient([page("ssr", "a", "b"), page("1", "b", "c"), page("2", "d")])
    result = run(tmp_path, client)

    written = list(read_jsonl(result.jobs_path))
    assert [r["objectID"] for r in written] == ["a", "b", "c", "d"]
    assert written[0]["payload"] == "raw-a"
    assert result.unique_records == 4


def test_single_duplicate_page_does_not_stop_the_run(tmp_path: Path) -> None:
    """Page 1 sometimes re-serves the SSR records; that is not exhaustion."""
    client = StubClient(
        [page("ssr", "a", "b"), page("1", "a", "b"), page("2", "c"), page("3", "d")]
    )
    result = run(tmp_path, client)

    assert client.consumed == 4
    assert result.unique_records == 4


def test_two_consecutive_duplicate_pages_stop_the_run(tmp_path: Path) -> None:
    client = StubClient(
        [page("ssr", "a"), page("1", "a"), page("2", "a"), page("3", "should-not-be-read")]
    )
    result = run(tmp_path, client)

    assert client.consumed == 3
    assert "consecutive" in result.stop_reason
    assert [r["objectID"] for r in read_jsonl(result.jobs_path)] == ["a"]


def test_empty_page_stops_the_run(tmp_path: Path) -> None:
    client = StubClient([page("ssr", "a"), page("1"), page("2", "b")])
    result = run(tmp_path, client)

    assert client.consumed == 2
    assert result.stop_reason == "empty page"


def test_a_source_that_ends_early_is_not_reported_as_the_ceiling(tmp_path: Path) -> None:
    """A capture file can end on a non-empty page; that is not truncation."""
    client = StubClient([page("ssr", "a"), page("1", "b")])
    result = run(tmp_path, client, max_pages=10)

    assert result.stop_reason == "no more pages"
    assert not result.truncated


def test_extra_meta_is_recorded(tmp_path: Path) -> None:
    result = run_scrape(
        [Search("local", {"q": 1})],
        tmp_path,
        client=StubClient([page("ssr", "a"), page("1")]),
        max_pages=5,
        compress=False,
        extra_meta={"source": "browser capture"},
    )

    assert read_meta(result.meta_path)["source"] == "browser capture"


def test_exhausting_the_client_reports_max_pages(tmp_path: Path) -> None:
    client = StubClient([page("ssr", "a"), page("1", "b")])
    result = run(tmp_path, client, max_pages=1)

    assert client.max_pages_seen == 1
    assert result.stop_reason == "reached max_pages=1"


def test_meta_captures_run_shape(tmp_path: Path) -> None:
    client = StubClient(
        [page("ssr", "a", "b", totals={"ssrTotalCount": 9}), page("1", "b", "c"), page("2")]
    )
    result = run(tmp_path, client)
    meta = read_meta(result.meta_path)
    [search] = meta["searches"]

    assert search["variant"] == "local"
    assert search["searchState"] == {"q": 1}
    assert search["pages"] == [
        {"page": "ssr", "received": 2, "new": 2},
        {"page": "1", "received": 2, "new": 1},
        {"page": "2", "received": 0, "new": 0},
    ]
    assert search["reported_totals"] == {"ssrTotalCount": 9}
    assert search["build_ids"] == ["build-1"]
    assert search["stop_reason"] == "empty page"
    assert search["already_written"] == 0
    assert meta["unique_records"] == 3
    assert meta["pages_fetched"] == 3
    assert meta["stop_reason"] == "empty page"
    assert meta["keyless_records"] == 0


def test_records_without_ids_are_kept_and_counted(tmp_path: Path) -> None:
    client = StubClient([ResultPage(label="ssr", records=[{"no": "id"}, {"no": "id"}])])
    result = run(tmp_path, client)
    meta = read_meta(result.meta_path)

    assert meta["keyless_records"] == 2
    assert len(list(read_jsonl(result.jobs_path))) == 2


def test_partial_results_and_meta_survive_a_failure(tmp_path: Path) -> None:
    client = StubClient([page("ssr", "a", "b")], error=HiringCafeError("boom"))

    with pytest.raises(HiringCafeError):
        run(tmp_path, client)

    jobs = next(iter(sorted(tmp_path.glob("jobs-*.jsonl"))))
    meta = read_meta(next(iter(sorted(tmp_path.glob("meta-*.json")))))
    assert [r["objectID"] for r in read_jsonl(jobs)] == ["a", "b"]
    assert meta["stop_reason"] == "error"
    assert meta["error"] == "boom"


def test_output_directory_is_created(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "raw"
    result = run_scrape(
        [Search("local", {"q": 1})], target, client=StubClient([page("ssr", "a")]), max_pages=1
    )

    assert result.jobs_path.parent == target


# ----- several searches --------------------------------------------------


class MultiStubClient:
    """Serves canned pages per search, keyed by the searchState's ``q``."""

    def __init__(self, pages: dict[str, list[ResultPage]], error_on: str | None = None) -> None:
        self.pages = pages
        self.error_on = error_on
        self.consumed: dict[str, int] = {}

    def iter_pages(self, search_state: Mapping[str, Any], max_pages: int) -> Iterator[ResultPage]:
        name = str(search_state["q"])
        self.consumed[name] = 0
        # Like the live client: the first page plus up to ``max_pages`` more.
        for item in self.pages[name][: max_pages + 1]:
            self.consumed[name] += 1
            yield item
        if name == self.error_on:
            raise HiringCafeError("boom")


def run_many(tmp_path: Path, client: Any, max_pages: int = 50) -> Any:
    searches = [Search(name, {"q": name}) for name in client.pages]
    return run_scrape(searches, tmp_path, client=client, max_pages=max_pages, compress=False)


def test_a_record_found_by_two_searches_is_written_once(tmp_path: Path) -> None:
    client = MultiStubClient(
        {"local": [page("ssr", "a", "b"), page("1")], "remote": [page("ssr", "b", "c"), page("1")]}
    )
    result = run_many(tmp_path, client)

    assert [r["objectID"] for r in read_jsonl(result.jobs_path)] == ["a", "b", "c"]
    assert result.unique_records == 3
    remote = result.searches[1]
    assert (remote.unique_records, remote.already_written) == (2, 1)


def test_overlap_with_an_earlier_search_does_not_stop_a_later_one(tmp_path: Path) -> None:
    """A nationwide search opens with remote jobs near home the local one found.

    Those pages are new to the remote search, so they must not count toward
    the consecutive-no-new stop rule.
    """
    client = MultiStubClient(
        {
            "local": [page("ssr", "a", "b"), page("1", "c", "d"), page("2")],
            "remote": [
                page("ssr", "a", "b"),
                page("1", "c", "d"),
                page("2", "e"),
                page("3"),
            ],
        }
    )
    result = run_many(tmp_path, client)

    assert client.consumed["remote"] == 4
    assert result.searches[1].stop_reason == "empty page"
    assert [r["objectID"] for r in read_jsonl(result.jobs_path)] == ["a", "b", "c", "d", "e"]


def test_meta_has_an_entry_per_search(tmp_path: Path) -> None:
    client = MultiStubClient(
        {
            "local": [page("ssr", "a", totals={"ssrTotalCount": 1}), page("1", "c")],
            "remote": [page("ssr", "a", "b", totals={"ssrTotalCount": 2}), page("1", "d")],
        }
    )
    result = run_many(tmp_path, client, max_pages=1)
    meta = read_meta(result.meta_path)

    assert [s["variant"] for s in meta["searches"]] == ["local", "remote"]
    assert [s["reported_totals"] for s in meta["searches"]] == [
        {"ssrTotalCount": 1},
        {"ssrTotalCount": 2},
    ]
    assert meta["searches"][1]["already_written"] == 1
    assert meta["unique_records"] == 4
    assert meta["stop_reason"] == "local: reached max_pages=1; remote: reached max_pages=1"
    assert result.truncated


def test_max_pages_applies_to_each_search(tmp_path: Path) -> None:
    client = MultiStubClient(
        {"local": [page("ssr", "a"), page("1", "b")], "remote": [page("ssr", "c"), page("1", "d")]}
    )
    run_many(tmp_path, client, max_pages=1)

    assert client.consumed == {"local": 2, "remote": 2}


def test_a_failing_later_search_keeps_earlier_records_and_names_itself(tmp_path: Path) -> None:
    client = MultiStubClient(
        {"local": [page("ssr", "a"), page("1")], "remote": [page("ssr", "b")]}, error_on="remote"
    )
    with pytest.raises(HiringCafeError):
        run_many(tmp_path, client)

    jobs = next(iter(sorted(tmp_path.glob("jobs-*.jsonl"))))
    meta = read_meta(next(iter(sorted(tmp_path.glob("meta-*.json")))))
    assert [r["objectID"] for r in read_jsonl(jobs)] == ["a", "b"]
    assert [s["stop_reason"] for s in meta["searches"]] == ["empty page", "error"]
    assert meta["error"] == "remote: boom"


def test_no_searches_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one search"):
        run_scrape([], tmp_path, client=StubClient([]), max_pages=1)


# ----- compression -------------------------------------------------------


def test_compressed_runs_write_a_gz_file_that_reads_back(tmp_path: Path) -> None:
    result = run(tmp_path, StubClient([page("ssr", "a", "b")]), compress=True)

    assert result.jobs_path.name.endswith(".jsonl.gz")
    assert gzip.decompress(result.jobs_path.read_bytes()).startswith(b"{")
    assert [record["objectID"] for record in read_jsonl(result.jobs_path)] == ["a", "b"]


def test_uncompressed_runs_write_a_plain_file(tmp_path: Path) -> None:
    result = run(tmp_path, StubClient([page("ssr", "a")]), compress=False)

    assert result.jobs_path.suffix == ".jsonl"
    assert result.jobs_path.read_text(encoding="utf-8").startswith("{")


def test_meta_stays_plain_and_names_the_records_file(tmp_path: Path) -> None:
    """The meta sidecar is read by eye after every run, so it is never gzipped."""
    result = run(tmp_path, StubClient([page("ssr", "a")]), compress=True)

    meta = read_meta(result.meta_path)
    assert result.meta_path.suffix == ".json"
    assert meta["compressed"] is True
    assert meta["jobs_file"] == result.jobs_path.name


def test_partial_compressed_output_survives_a_failure(tmp_path: Path) -> None:
    """A gzip flush is a sync point, so an aborted run still decompresses."""
    client = StubClient([page("ssr", "a")], error=HiringCafeError("boom"))
    with pytest.raises(HiringCafeError):
        run(tmp_path, client, compress=True)

    written = sorted(tmp_path.glob("jobs-*.jsonl.gz"))
    assert [record["objectID"] for record in read_jsonl(written[0])] == ["a"]
