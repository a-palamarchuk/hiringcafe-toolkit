"""Tests for job-shortlist stage 4.

Concentrated on the two ways this stage fails invisibly: a posting whose
identity shifts between runs and so reappears every day, and a store write that
loses state nobody notices is gone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hiringcafe_toolkit.common.jsonl import read_jsonl
from hiringcafe_toolkit.job_shortlist.diff import (
    SeenEntry,
    SeenStore,
    load_opened_postings,
    load_seen_store,
    match_keys,
    run_diff,
    save_seen_store,
)
from hiringcafe_toolkit.job_shortlist.normalize import Posting
from hiringcafe_toolkit.job_shortlist.screen import BAND_POSSIBLE, BAND_REJECTED, BAND_STRONG

TODAY = "2026-08-23"


def posting(object_id: str = "a", **overrides: Any) -> Posting:
    defaults: dict[str, Any] = {
        "object_id": object_id,
        "cluster_key": None,
        "dedup_key": "k",
        "fallback_key": "Acme|software engineer|Reston",
        "title": "Software Engineer",
        "raw_title": "Senior Software Engineer",
        "company": "Acme",
        "company_host": "acme.com",
        "comp_max": 200_000,
    }
    return Posting(**{**defaults, **overrides})


def screened(object_id: str = "a", band: str = BAND_STRONG, **overrides: Any) -> dict[str, Any]:
    return {
        **posting(object_id, **overrides).as_record(),
        "band": band,
        "reject_reasons": [],
        "demote_reasons": [],
    }


# ----- identity -----------------------------------------------------------


def test_a_posting_is_known_by_every_merged_id() -> None:
    """The surviving listing depends on source and date, so it can change."""
    keys = match_keys(posting("a", alternate_object_ids=("b", "c")))

    assert {"id:a", "id:b", "id:c"} <= set(keys)


def test_the_cluster_key_is_a_match_key() -> None:
    assert "cluster:c1" in match_keys(posting(cluster_key="c1"))


def test_content_derived_keys_are_not_matched_on() -> None:
    """They collide across distinct postings and would undo the normalize stage.

    Measured on 116 surfaced postings: a company-title-level key merged Capital
    One's front-end and back-end Lead Software Engineer roles, and Exiger's
    Tech Lead with its Database Engineer.
    """
    keys = match_keys(posting(cluster_key="c1"))

    assert not any(k.startswith(("fallback:", "repost:")) for k in keys)


def test_two_roles_sharing_company_title_and_level_stay_separate(tmp_path: Path) -> None:
    """Capital One's front-end and back-end Lead Software Engineer, in miniature."""
    store = tmp_path / "seen.jsonl"
    front = screened("front", title="Software Engineer", raw_title="Sr. Lead SWE, Front End")
    back = screened("back", title="Software Engineer", raw_title="Sr. Lead SWE - Back End")
    run_diff([front], tmp_path, store, compress=False)
    second = run_diff([back], tmp_path, store, compress=False)

    assert second.surfaced == 1
    assert second.suppressed == 0


# ----- suppression --------------------------------------------------------


def test_a_previously_surfaced_posting_is_suppressed(tmp_path: Path) -> None:
    store = tmp_path / "seen.jsonl"
    run_diff([screened()], tmp_path, store, compress=False)
    second = run_diff([screened()], tmp_path, store, compress=False)

    assert second.surfaced == 0
    assert second.suppressed == 1


def test_a_changed_surviving_id_does_not_resurface_the_posting(tmp_path: Path) -> None:
    """Tomorrow's merge can pick a different representative for the same job."""
    store = tmp_path / "seen.jsonl"
    run_diff([screened("a", alternate_object_ids=("b",))], tmp_path, store, compress=False)
    second = run_diff([screened("b", alternate_object_ids=("a",))], tmp_path, store, compress=False)

    assert second.suppressed == 1


def test_a_posting_that_gains_a_cluster_key_does_not_resurface(tmp_path: Path) -> None:
    store = tmp_path / "seen.jsonl"
    run_diff([screened("a")], tmp_path, store, compress=False)
    second = run_diff([screened("a", cluster_key="c1")], tmp_path, store, compress=False)

    assert second.suppressed == 1


def test_a_posting_whose_every_id_changed_is_treated_as_new(tmp_path: Path) -> None:
    """Deliberate: an employer re-listing under fresh requisitions is worth seeing."""
    store = tmp_path / "seen.jsonl"
    run_diff([screened("old")], tmp_path, store, compress=False)
    second = run_diff([screened("new")], tmp_path, store, compress=False)

    assert second.surfaced == 1


def test_a_genuinely_new_posting_is_surfaced(tmp_path: Path) -> None:
    store = tmp_path / "seen.jsonl"
    run_diff([screened("a")], tmp_path, store, compress=False)
    second = run_diff(
        [
            screened(
                "b",
                fallback_key="Other|data engineer|Reston",
                title="Data Engineer",
                raw_title="Senior Data Engineer",
                company="Other",
                company_host="other.com",
            )
        ],
        tmp_path,
        store,
        compress=False,
    )

    assert second.surfaced == 1
    assert second.new_postings == 1


# ----- promotion ----------------------------------------------------------


def test_a_promotion_resurfaces_the_posting(tmp_path: Path) -> None:
    """A rule change that improves a band must not be hidden by the store."""
    store = tmp_path / "seen.jsonl"
    run_diff([screened(band=BAND_POSSIBLE)], tmp_path, store, compress=False)
    second = run_diff([screened(band=BAND_STRONG)], tmp_path, store, compress=False)

    assert second.surfaced == 1
    assert second.promoted == 1


def test_a_demotion_does_not_resurface(tmp_path: Path) -> None:
    store = tmp_path / "seen.jsonl"
    run_diff([screened(band=BAND_STRONG)], tmp_path, store, compress=False)
    second = run_diff([screened(band=BAND_POSSIBLE)], tmp_path, store, compress=False)

    assert second.suppressed == 1


def test_a_posting_promoted_once_is_not_promoted_again(tmp_path: Path) -> None:
    store = tmp_path / "seen.jsonl"
    run_diff([screened(band=BAND_POSSIBLE)], tmp_path, store, compress=False)
    run_diff([screened(band=BAND_STRONG)], tmp_path, store, compress=False)
    third = run_diff([screened(band=BAND_STRONG)], tmp_path, store, compress=False)

    assert third.surfaced == 0


# ----- what the store holds ----------------------------------------------


def test_rejected_postings_are_never_surfaced_or_stored(tmp_path: Path) -> None:
    """They were never shown, so a later promotion should surface them normally."""
    store = tmp_path / "seen.jsonl"
    result = run_diff([screened(band=BAND_REJECTED)], tmp_path, store, compress=False)

    assert result.surfaced == 0
    assert result.store_size == 0

    later = run_diff([screened(band=BAND_STRONG)], tmp_path, store, compress=False)
    assert later.surfaced == 1


def test_the_store_accumulates_keys_from_suppressed_postings(tmp_path: Path) -> None:
    """Identity drift would otherwise outrun the store.

    Run one knows a posting as {a, b}, run two as {b, c}, run three as {c, d}.
    A store learning only from surfaced postings still holds {a, b} on run
    three, and the posting comes back as new.
    """
    store_path = tmp_path / "seen.jsonl"
    run_diff([screened("a", alternate_object_ids=("b",))], tmp_path, store_path, compress=False)
    run_diff([screened("b", alternate_object_ids=("c",))], tmp_path, store_path, compress=False)

    entry = load_seen_store(store_path).entries()[0]
    assert {"id:a", "id:b", "id:c"} <= set(entry.keys)


def test_identity_drift_over_three_runs_does_not_resurface(tmp_path: Path) -> None:
    store_path = tmp_path / "seen.jsonl"
    run_diff([screened("a", alternate_object_ids=("b",))], tmp_path, store_path, compress=False)
    run_diff([screened("b", alternate_object_ids=("c",))], tmp_path, store_path, compress=False)
    third = run_diff(
        [
            screened(
                "c",
                alternate_object_ids=("d",),
                fallback_key="moved",
                cluster_key="c9",
                raw_title="Staff Software Engineer",
            )
        ],
        tmp_path,
        store_path,
        compress=False,
    )

    assert third.suppressed == 1


def test_a_suppressed_posting_does_not_inflate_its_surfaced_count(tmp_path: Path) -> None:
    """It was seen in the scrape, not shown to anyone."""
    store_path = tmp_path / "seen.jsonl"
    run_diff([screened()], tmp_path, store_path, compress=False)
    run_diff([screened()], tmp_path, store_path, compress=False)

    assert load_seen_store(store_path).entries()[0].surfaced_count == 1


def test_first_surfaced_is_preserved_while_last_surfaced_moves(tmp_path: Path) -> None:
    store_path = tmp_path / "seen.jsonl"
    run_diff([screened(band=BAND_POSSIBLE)], tmp_path, store_path, compress=False)
    run_diff([screened(band=BAND_STRONG)], tmp_path, store_path, compress=False)

    entry = load_seen_store(store_path).entries()[0]
    assert entry.surfaced_count == 2
    assert entry.band == BAND_STRONG
    assert entry.first_surfaced and entry.last_surfaced


def test_dry_run_leaves_the_store_untouched(tmp_path: Path) -> None:
    store_path = tmp_path / "seen.jsonl"
    result = run_diff([screened()], tmp_path, store_path, compress=False, dry_run=True)

    assert result.surfaced == 1
    assert not store_path.exists()


# ----- store durability ---------------------------------------------------


def test_a_missing_store_reads_as_empty(tmp_path: Path) -> None:
    assert len(load_seen_store(tmp_path / "absent.jsonl")) == 0


def test_the_store_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "seen.jsonl"
    entry = SeenEntry(
        object_id="a",
        keys=("id:a", "repost:x"),
        band=BAND_STRONG,
        first_surfaced=TODAY,
        last_surfaced=TODAY,
        title="Senior Software Engineer",
        company="Acme",
    )
    save_seen_store(SeenStore([entry]), path)

    assert load_seen_store(path).entries() == [entry]


def test_the_store_is_written_atomically(tmp_path: Path) -> None:
    """Losing this file is silent and expensive, so a crash must not truncate it."""
    path = tmp_path / "seen.jsonl"
    save_seen_store(SeenStore([SeenEntry("a", ("id:a",), BAND_STRONG, TODAY, TODAY)]), path)
    original = path.read_text(encoding="utf-8")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "hiringcafe_toolkit.job_shortlist.diff.os.replace",
            lambda *_: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError):
            save_seen_store(SeenStore([SeenEntry("b", ("id:b",), BAND_STRONG, TODAY, TODAY)]), path)

    assert path.read_text(encoding="utf-8") == original


def test_one_line_per_posting_however_often_it_is_surfaced(tmp_path: Path) -> None:
    store_path = tmp_path / "seen.jsonl"
    for _ in range(4):
        run_diff([screened()], tmp_path, store_path, compress=False)

    assert len(list(read_jsonl(store_path))) == 1


# ----- visit log labels ---------------------------------------------------


def visit_log(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "visits.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_only_prefixed_keys_are_read_from_the_export() -> None:
    path = Path("/tmp/vl.json")
    path.write_text(
        json.dumps({"acme.com": {"date": "2026-08-01"}, "job:abc": {"date": "2026-08-02"}}),
        encoding="utf-8",
    )

    assert load_opened_postings(path) == {"job:abc": False}


def test_posting_keys_are_read_case_sensitively() -> None:
    """Ids are case-sensitive; lower-casing would silently break every lookup."""
    path = Path("/tmp/vl2.json")
    path.write_text(json.dumps({"job:AbC": {"date": "x", "r": True}}), encoding="utf-8")

    assert load_opened_postings(path) == {"job:AbC": True}


def test_opened_and_applied_labels_attach_to_the_store(tmp_path: Path) -> None:
    store_path = tmp_path / "seen.jsonl"
    run_diff([screened("a")], tmp_path, store_path, compress=False)
    log = visit_log(tmp_path, {"job:a": {"date": "2026-08-23", "r": True}})
    result = run_diff([screened("a")], tmp_path, store_path, visit_log_path=log, compress=False)

    assert (result.opened, result.applied) == (1, 1)
    entry = load_seen_store(store_path).entries()[0]
    assert entry.opened and entry.applied


def test_a_label_attaches_through_an_alternate_id(tmp_path: Path) -> None:
    """The render wrote whichever id survived that day, which may have changed."""
    store_path = tmp_path / "seen.jsonl"
    run_diff([screened("a", alternate_object_ids=("b",))], tmp_path, store_path, compress=False)
    log = visit_log(tmp_path, {"job:b": {"date": "2026-08-23"}})
    result = run_diff([screened("a")], tmp_path, store_path, visit_log_path=log, compress=False)

    assert result.opened == 1
    assert result.applied == 0


def test_labels_do_not_affect_suppression(tmp_path: Path) -> None:
    """Suppression is the store's business alone, so a browser gap cannot hide postings."""
    store_path = tmp_path / "seen.jsonl"
    run_diff([screened("a")], tmp_path, store_path, compress=False)
    log = visit_log(tmp_path, {})
    result = run_diff([screened("a")], tmp_path, store_path, visit_log_path=log, compress=False)

    assert result.suppressed == 1


# ----- output -------------------------------------------------------------


def test_the_shortlist_carries_bands_and_is_ordered_strong_first(tmp_path: Path) -> None:
    records = [
        screened("p", band=BAND_POSSIBLE, fallback_key="p", title="A", company="P"),
        screened("s", band=BAND_STRONG, fallback_key="s", title="B", company="S"),
    ]
    result = run_diff(records, tmp_path, tmp_path / "seen.jsonl", compress=False)

    assert [row["band"] for row in read_jsonl(result.shortlist_path)] == [
        BAND_STRONG,
        BAND_POSSIBLE,
    ]


def test_meta_records_the_counts(tmp_path: Path) -> None:
    result = run_diff([screened()], tmp_path, tmp_path / "seen.jsonl", compress=False)
    meta: Any = json.loads(result.meta_path.read_text(encoding="utf-8"))

    assert meta["surfaced"] == 1
    assert meta["of_which_new"] == 1
    assert meta["store_entries_after"] == 1
