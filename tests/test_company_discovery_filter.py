"""Tests for company-discovery stage 3 (visited filter)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hiringcafe_toolkit.company_discovery.visited_filter import (
    VisitLog,
    VisitLogError,
    filter_companies,
    load_visit_log,
    run_filter,
)

JsonDict = dict[str, Any]


def company(host: str | None, name: str = "Co") -> JsonDict:
    return {"name": name, "website_host": host, "careers_url": "https://x.example/"}


def write_log(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "visitlog.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def log_of(*hosts: str) -> VisitLog:
    return VisitLog(
        hosts=frozenset(hosts),
        applied_hosts=frozenset(),
        entries_read=len(hosts),
        unusable_keys=(),
    )


# ----- loading the export ------------------------------------------------


def test_load_visit_log_reads_the_export_shape(tmp_path: Path) -> None:
    path = write_log(
        tmp_path,
        {
            "1password.com": {"date": "2026-07-10", "r": True},
            "6sense.com": {"date": "2026-07-10"},
            "abnormal.ai": {"date": "2026-06-13"},
        },
    )
    log = load_visit_log(path)

    assert log.hosts == {"1password.com", "6sense.com", "abnormal.ai"}
    assert log.applied_hosts == {"1password.com"}
    assert log.entries_read == 3


def test_load_visit_log_normalizes_keys(tmp_path: Path) -> None:
    """Export keys and company hosts go through the same normalization."""
    path = write_log(
        tmp_path,
        {"WWW.Acme.com": {"date": "2026-01-01"}, "https://beta.io/careers": {}},
    )
    log = load_visit_log(path)

    assert log.hosts == {"acme.com", "beta.io"}


def test_load_visit_log_reports_unusable_keys(tmp_path: Path) -> None:
    path = write_log(tmp_path, {"acme.com": {}, "localhost": {}, "": {}})
    log = load_visit_log(path)

    assert log.hosts == {"acme.com"}
    assert len(log.unusable_keys) == 2


def test_load_visit_log_tolerates_missing_metadata(tmp_path: Path) -> None:
    path = write_log(tmp_path, {"acme.com": None, "beta.io": "2026-01-01"})
    log = load_visit_log(path)

    assert log.hosts == {"acme.com", "beta.io"}
    assert log.applied_hosts == frozenset()


@pytest.mark.parametrize(
    ("payload", "message"), [("[]", "expected a JSON object"), ("{oops", "invalid JSON")]
)
def test_load_visit_log_rejects_bad_input(tmp_path: Path, payload: str, message: str) -> None:
    path = tmp_path / "visitlog.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(VisitLogError, match=message):
        load_visit_log(path)


def test_load_visit_log_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(VisitLogError, match="not found"):
        load_visit_log(tmp_path / "absent.json")


# ----- filtering ---------------------------------------------------------


def test_visited_companies_are_excluded() -> None:
    kept, excluded, stats = filter_companies(
        [company("acme.com"), company("beta.io"), company("gamma.dev")],
        log_of("beta.io"),
    )

    assert [c["website_host"] for c in kept] == ["acme.com", "gamma.dev"]
    assert [c["website_host"] for c in excluded] == ["beta.io"]
    assert stats["excluded_visited"] == 1


def test_matching_is_normalization_insensitive() -> None:
    kept, excluded, _ = filter_companies([company("Acme.com")], log_of("acme.com"))

    assert kept == []
    assert len(excluded) == 1


def test_companies_without_a_host_are_kept() -> None:
    """Nothing to match on, so the row survives rather than being silently lost."""
    kept, excluded, stats = filter_companies([company(None)], log_of("acme.com"))

    assert len(kept) == 1
    assert excluded == []
    assert stats["kept_unmatchable"] == 1


def test_applied_flag_is_counted_but_does_not_change_exclusion() -> None:
    visit_log = VisitLog(
        hosts=frozenset({"acme.com", "beta.io"}),
        applied_hosts=frozenset({"acme.com"}),
        entries_read=2,
        unusable_keys=(),
    )
    kept, excluded, stats = filter_companies([company("acme.com"), company("beta.io")], visit_log)

    assert kept == []
    assert len(excluded) == 2
    assert stats["excluded_already_applied"] == 1


def test_empty_visit_log_keeps_everything() -> None:
    kept, excluded, _ = filter_companies([company("acme.com")], log_of())

    assert len(kept) == 1
    assert excluded == []


# ----- file output -------------------------------------------------------


def test_run_filter_writes_files_and_meta(tmp_path: Path) -> None:
    result = run_filter(
        [company("acme.com"), company("beta.io")],
        log_of("beta.io"),
        tmp_path,
        tail=[company(None, name="No Site Co")],
        source_path=Path("companies.jsonl"),
        visit_log_path=Path("visitlog.json"),
    )

    remaining = [json.loads(line) for line in result.remaining_path.read_text().splitlines()]
    meta = json.loads(result.meta_path.read_text())

    assert [c["website_host"] for c in remaining] == ["acme.com"]
    assert result.kept == 1 and result.excluded == 1 and result.passthrough == 1
    assert meta["source"] == "companies.jsonl"
    assert meta["visit_log"] == "visitlog.json"
    assert meta["excluded_sample"] == ["beta.io"]
    assert meta["stats"]["passthrough_no_website"] == 1


def test_tail_passes_through_unfiltered(tmp_path: Path) -> None:
    """Website-less rows cannot be matched against a host-keyed visit log."""
    result = run_filter(
        [],
        log_of("acme.com"),
        tmp_path,
        tail=[company(None, name="A"), company(None, name="B")],
    )
    tail_file = result.remaining_path.parent / result.remaining_path.name.replace(
        "remaining-", "remaining-no-website-"
    )

    assert result.passthrough == 2
    assert len(tail_file.read_text().splitlines()) == 2
