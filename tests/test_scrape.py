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
from hiringcafe_toolkit.common.scrape import run_scrape

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
    return run_scrape({"q": 1}, tmp_path, client=client, max_pages=max_pages, compress=compress)


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

    assert meta["searchState"] == {"q": 1}
    assert meta["pages"] == [
        {"page": "ssr", "received": 2, "new": 2},
        {"page": "1", "received": 2, "new": 1},
        {"page": "2", "received": 0, "new": 0},
    ]
    assert meta["reported_totals"] == {"ssrTotalCount": 9}
    assert meta["unique_records"] == 3
    assert meta["build_ids"] == ["build-1"]
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
    result = run_scrape({"q": 1}, target, client=StubClient([page("ssr", "a")]), max_pages=1)

    assert result.jobs_path.parent == target


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
