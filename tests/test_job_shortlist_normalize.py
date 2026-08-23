"""Tests for job-shortlist stage 2.

Focused on the decisions that silently lose postings: which fields survive
flattening, and which listings get merged into one another.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.common.jsonl import read_jsonl
from hiringcafe_toolkit.job_shortlist.normalize import (
    DEFAULT_SIMILARITY,
    Posting,
    calibrate_similarity,
    deduplicate,
    fallback_key,
    level_signature,
    normalize_record,
    normalized_title,
    run_normalize,
    similarity,
)

JsonDict = dict[str, Any]

LOREM = "python kubernetes terraform aws distributed systems backend platform services"


def raw(
    object_id: str,
    *,
    title: str = "Software Engineer",
    company: str = "Acme",
    cluster: str | None = None,
    source: str = "grnhse",
    requirements: str = LOREM,
    cities: list[str] | None = None,
    published: str = "2026-08-20T00:00:00Z",
    comp_max: Any = 200_000,
    apply_url: str = "https://example.test/a",
    raw_title: str | None = None,
    **job_fields: Any,
) -> JsonDict:
    record: JsonDict = {
        "objectID": object_id,
        "source": source,
        "apply_url": apply_url,
        "is_expired": False,
        "v5_processed_job_data": {
            "core_job_title": title,
            "company_name": company,
            "requirements_summary": requirements,
            "workplace_cities": cities if cities is not None else ["Reston, Virginia, US"],
            "estimated_publish_date": published,
            "yearly_max_compensation": comp_max,
            "commitment": ["Full Time"],
            "technical_tools": ["Python", "AWS"],
            **job_fields,
        },
        "enriched_company_data": {"name": company, "homepage_uri": "www.Acme.com/"},
        "job_information": {"title": raw_title if raw_title is not None else title},
    }
    if cluster is not None:
        record["liberal_dedup_cluster"] = cluster
    return record


def posting(object_id: str, **kwargs: Any) -> Posting:
    return normalize_record(raw(object_id, **kwargs))


# ----- flattening ---------------------------------------------------------


def test_nested_vendor_fields_are_projected_onto_the_flat_schema() -> None:
    result = normalize_record(
        raw("a", seniority_level="Senior Level", role_type="Individual Contributor")
    )

    assert result.object_id == "a"
    assert result.title == "Software Engineer"
    assert result.seniority_level == "Senior Level"
    assert result.role_type == "Individual Contributor"
    assert result.cities == ("Reston, Virginia, US",)
    assert result.city_count == 1


def test_company_host_is_normalized_for_matching() -> None:
    assert normalize_record(raw("a")).company_host == "acme.com"


def test_implausible_compensation_is_dropped_and_flagged() -> None:
    """A stated-but-absurd figure must not read the same as an unstated one."""
    absurd = normalize_record(raw("a", comp_max=81_337_000))
    silent = normalize_record(raw("b", comp_max=None))

    assert absurd.comp_max is None and absurd.comp_implausible is True
    assert silent.comp_max is None and silent.comp_implausible is False


def test_missing_title_falls_back_to_job_information() -> None:
    record = raw("a")
    record["v5_processed_job_data"]["core_job_title"] = ""
    record["job_information"] = {"title": "Backend Engineer"}

    assert normalize_record(record).title == "Backend Engineer"


def test_absent_seniority_stays_none_rather_than_defaulting() -> None:
    """Absent means extraction failed; a default would read as a real level."""
    assert normalize_record(raw("a")).seniority_level is None


def test_normalized_title_collapses_whitespace_and_case() -> None:
    assert normalized_title("  Software   Engineer II ") == "software engineer ii"


def test_raw_title_is_kept_because_core_title_loses_the_level() -> None:
    result = normalize_record(raw("a", title="DevOps Engineer", raw_title="Senior DevOps Engineer"))

    assert result.title == "DevOps Engineer"
    assert result.raw_title == "Senior DevOps Engineer"


# ----- level signatures ---------------------------------------------------


def test_level_signature_reads_numeric_and_word_levels() -> None:
    assert level_signature("Electrical Commissioning Engineer II") == "ii"
    assert level_signature("Electrical Commissioning Engineer I") == "i"
    assert level_signature("Engineer 1_5") == "1.5"
    assert level_signature("Principal FPGA Engineer") == "principal"
    assert level_signature("Engineer L4") == "4"


def test_level_signature_normalizes_seniority_spellings() -> None:
    assert level_signature("Sr. Software Engineer") == level_signature("Senior Software Engineer")


def test_level_signature_is_order_independent() -> None:
    assert level_signature("Senior Engineer II") == level_signature("Engineer II Senior")


def test_level_signature_is_empty_when_no_level_is_stated() -> None:
    assert level_signature("Software Engineer") == ""
    assert level_signature("Software Engineer (C++)") == ""


# ----- similarity and calibration ----------------------------------------


def test_similarity_is_symmetric_and_bounded() -> None:
    assert similarity("alpha beta", "alpha beta") == 1.0
    assert similarity("alpha beta", "gamma delta") == 0.0
    assert similarity("", "alpha") == 0.0
    assert similarity("alpha beta", "beta gamma") == similarity("beta gamma", "alpha beta")


def test_calibration_falls_back_when_ground_truth_is_thin() -> None:
    postings = [posting(str(n), cluster="c1") for n in range(4)]
    threshold, groups = calibrate_similarity(postings)

    assert threshold == DEFAULT_SIMILARITY
    assert groups == 1


def test_calibration_uses_cluster_groups_when_there_are_enough() -> None:
    postings: list[Posting] = []
    for n in range(12):
        postings.append(posting(f"{n}a", cluster=f"c{n}", requirements=f"{LOREM} extra{n}"))
        postings.append(posting(f"{n}b", cluster=f"c{n}", requirements=f"{LOREM} other{n}"))
    threshold, groups = calibrate_similarity(postings)

    assert groups == 12
    assert 0.0 < threshold <= 1.0
    assert threshold != DEFAULT_SIMILARITY


# ----- duplicate collapsing ----------------------------------------------


def test_cluster_key_merges_are_trusted_without_a_text_check() -> None:
    """The API's own key is authoritative even when the wording diverges."""
    members = [
        posting("a", cluster="c1", requirements="alpha beta gamma"),
        posting("b", cluster="c1", requirements="nothing alike whatsoever"),
    ]
    kept, stats = deduplicate(members, 0.9)

    assert len(kept) == 1
    assert kept[0].duplicate_count == 2
    assert stats["merged_by_cluster_key"] == 1


def test_fallback_merges_listings_whose_text_agrees() -> None:
    members = [posting("a", apply_url="https://one.test"), posting("b", source="workday")]
    kept, stats = deduplicate(members, 0.5)

    assert len(kept) == 1
    assert stats["merged_by_fallback"] == 1
    assert kept[0].duplicate_count == 2


def test_fallback_declines_to_merge_when_text_disagrees() -> None:
    """Two distinct roles sharing a title must survive as two postings."""
    members = [
        posting("a", requirements="kernel drivers embedded firmware rtos"),
        posting("b", requirements="react frontend css design accessibility"),
    ]
    kept, stats = deduplicate(members, 0.5)

    assert len(kept) == 2
    assert stats["fallback_merges_declined"] == 1
    assert stats.get("merged_by_fallback", 0) == 0


def test_fallback_key_ignores_source_so_cross_ats_duplicates_still_merge() -> None:
    """Including source would block precisely the merges the key exists for."""
    assert posting("a", source="icims").fallback_key == posting("b", source="icims2").fallback_key


def test_fallback_key_is_recorded_even_when_a_cluster_key_was_used() -> None:
    """Cluster coverage varies between runs, so a posting can lose its key."""
    result = posting("a", cluster="c1")

    assert result.cluster_key == "c1"
    assert result.fallback_key == fallback_key(
        "Acme", "Software Engineer", ["Reston, Virginia, US"]
    )


def test_fallback_key_is_stable_against_city_ordering() -> None:
    left = fallback_key("Acme", "Engineer", ["Boston, MA, US", "Reston, VA, US"])
    right = fallback_key("Acme", "Engineer", ["Reston, VA, US", "Boston, MA, US"])

    assert left == right


def test_merged_postings_keep_every_object_id() -> None:
    """The surviving listing depends on source and date, so it can change."""
    members = [
        posting("a", source="adhoc"),
        posting("b", source="workday"),
        posting("c", source="grnhse"),
    ]
    kept, _ = deduplicate(members, 0.5)

    assert len(kept) == 1
    assert kept[0].object_id == "b"
    assert set(kept[0].alternate_object_ids) == {"a", "c"}


def test_an_unmerged_posting_has_no_alternate_ids() -> None:
    kept, _ = deduplicate([posting("a")], 0.5)

    assert kept[0].alternate_object_ids == ()


def test_level_variants_of_one_role_are_not_merged() -> None:
    """The failure this guard exists for.

    Four Bureau Veritas requisitions at levels I, 1.5 and II shared a company,
    a city, a core title and near-identical boilerplate, and collapsed into
    one posting whose surviving level was the wrong one.
    """
    shared = "requires 1-2 years experience drivers license cpr first aid osha nfpa"
    members = [
        posting("a", title="Engineer", raw_title="Engineer I", requirements=shared),
        posting("b", title="Engineer", raw_title="Engineer II", requirements=shared),
        posting("c", title="Engineer", raw_title="Engineer 1_5", requirements=shared),
    ]
    kept, stats = deduplicate(members, 0.5)

    assert len(kept) == 3
    assert stats["fallback_merges_declined"] == 2


def test_same_level_listings_still_merge() -> None:
    """The guard must not block ordinary cross-ATS duplicates."""
    members = [
        posting("a", title="Engineer", raw_title="Sr. Engineer", source="icims"),
        posting("b", title="Engineer", raw_title="Senior Engineer", source="workday"),
    ]
    kept, _ = deduplicate(members, 0.5)

    assert len(kept) == 1


def test_an_unlevelled_title_does_not_merge_with_a_levelled_one() -> None:
    """ "Software Engineer" and "Senior Software Engineer" are different jobs."""
    members = [
        posting("a", title="Engineer", raw_title="Engineer"),
        posting("b", title="Engineer", raw_title="Senior Engineer"),
    ]
    kept, _ = deduplicate(members, 0.5)

    assert len(kept) == 2


def test_cluster_key_merges_ignore_the_level_guard() -> None:
    """The API's own key stays authoritative; the guard is fallback-only."""
    members = [
        posting("a", cluster="c1", raw_title="Engineer I"),
        posting("b", cluster="c1", raw_title="Engineer II"),
    ]
    kept, _ = deduplicate(members, 0.5)

    assert len(kept) == 1


def test_differing_cities_are_separate_candidates() -> None:
    members = [
        posting("a", cities=["Reston, Virginia, US"]),
        posting("b", cities=["Austin, Texas, US"]),
    ]
    kept, _ = deduplicate(members, 0.0)

    assert len(kept) == 2


def test_postings_without_requirements_text_are_not_merged() -> None:
    """An unverifiable merge is declined rather than assumed."""
    members = [posting("a", requirements=""), posting("b", requirements="")]
    kept, _ = deduplicate(members, 0.5)

    assert len(kept) == 2


def test_employer_ats_is_kept_over_an_aggregator_copy() -> None:
    members = [
        posting("a", source="adhoc", apply_url="https://aggregator.test"),
        posting("b", source="workday", apply_url="https://employer.test"),
    ]
    kept, _ = deduplicate(members, 0.5)

    assert kept[0].source == "workday"
    assert kept[0].apply_url == "https://employer.test"
    assert kept[0].alternate_apply_urls == ("https://aggregator.test",)


def test_newer_listing_wins_between_equal_sources() -> None:
    members = [
        posting("a", published="2026-08-01T00:00:00Z"),
        posting("b", published="2026-08-20T00:00:00Z"),
    ]
    kept, _ = deduplicate(members, 0.5)

    assert kept[0].object_id == "b"


def test_alternate_apply_urls_survive_the_merge() -> None:
    """The surviving link is a heuristic and occasionally 404s."""
    members = [
        posting("a", source="workday", apply_url="https://one.test"),
        posting("b", source="grnhse", apply_url="https://two.test"),
    ]
    kept, _ = deduplicate(members, 0.5)

    assert kept[0].alternate_apply_urls == ("https://two.test",)


# ----- stage output -------------------------------------------------------


def test_run_writes_postings_and_a_meta_sidecar(tmp_path: Path) -> None:
    records = [raw("a", cluster="c1"), raw("b", cluster="c1"), raw("c", title="Data Engineer")]
    result = run_normalize(records, tmp_path, compress=False)

    written = list(read_jsonl(result.postings_path))
    assert len(written) == result.postings_out == 2
    assert result.postings_in == 3

    meta: Any = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert meta["postings_file"] == result.postings_path.name
    assert meta["similarity_overridden"] is False
    assert meta["cluster_key_coverage"] > 0


def test_run_honors_a_threshold_override(tmp_path: Path) -> None:
    result = run_normalize([raw("a")], tmp_path, compress=False, threshold=0.7)

    meta: Any = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert result.threshold == 0.7
    assert meta["similarity_overridden"] is True


def test_run_compresses_by_default(tmp_path: Path) -> None:
    result = run_normalize([raw("a")], tmp_path)

    assert result.postings_path.name.endswith(".jsonl.gz")
    assert len(list(read_jsonl(result.postings_path))) == 1


def test_output_is_ordered_newest_first(tmp_path: Path) -> None:
    records = [
        raw("old", title="A", published="2026-08-01T00:00:00Z"),
        raw("new", title="B", published="2026-08-20T00:00:00Z"),
    ]
    result = run_normalize(records, tmp_path, compress=False)

    assert [r["object_id"] for r in read_jsonl(result.postings_path)] == ["new", "old"]
