"""Stage 4 of job shortlist: suppress what has already been surfaced.

The store answers one question - has this posting been put in front of me
before - and that is harder than it looks, because a posting's identity is not
stable between runs.

``object_id`` names whichever listing survived a merge, and which one survives
depends on source preference and publish date, so a new listing arriving
tomorrow can displace today's survivor. So matching is on a *set* of ids -
every listing merged into the posting, plus the vendor's cluster key - and a
hit on any one suppresses.

Content-derived keys were tried and rejected; see ``match_keys`` for the
measurements. The short version is that suppressing a distinct posting removes
it from every future render and nothing says so, while failing to suppress a
repost costs one glance.

Suppression is deliberately independent of the browser. The store records what
the pipeline *surfaced*; VisitLogger records what was *opened*. Making
suppression depend on both would mean a browser-side gap could silently hide
postings. Here the visit log only attaches labels, so forgetting to run the
tab queue costs data for the eval set and never breaks the feed.

Rejected postings are never stored. They were never shown, and leaving them out
means a rule change that promotes one surfaces it on the next run with no
special handling.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.common.jsonl import (
    JsonlWriter,
    read_jsonl,
    unique_path,
    with_compression,
)
from hiringcafe_toolkit.common.urls import POSTING_KEY_PREFIX, applied_to, posting_key
from hiringcafe_toolkit.job_shortlist.normalize import Posting
from hiringcafe_toolkit.job_shortlist.screen import BAND_POSSIBLE, BAND_STRONG, Verdict

JsonDict = dict[str, Any]

#: Bands worth surfacing, best first. Anything else was never shown, so it is
#: never recorded.
SURFACED_BANDS = (BAND_STRONG, BAND_POSSIBLE)

#: Rank used to decide whether a band improved since a posting was last shown.
#: A posting shown as `possible` and later promoted to `strong` comes back.
BAND_RANK = {BAND_STRONG: 2, BAND_POSSIBLE: 1}

#: Fields the screen stage adds on top of a posting.
VERDICT_FIELDS = frozenset({"band", "reject_reasons", "demote_reasons"})

logger = logging.getLogger(__name__)


def _better_band(candidate: str, current: str) -> str:
    """The stronger of two bands, so a promotion is remembered."""
    return candidate if BAND_RANK.get(candidate, 0) > BAND_RANK.get(current, 0) else current


def match_keys(posting: Posting) -> tuple[str, ...]:
    """Every identity a posting can be recognized by, deduplicated and sorted.

    Only identifiers naming *this listing* are used: the ids of every listing
    merged into it, and the vendor's cluster key.

    Content-derived keys are deliberately excluded, having been tried and
    measured. A key of company, title and level collides across genuinely
    distinct postings at a rate that matters - among 116 surfaced postings it
    merged Capital One's "Senior Lead Software Engineer, Front End Web" with
    its "Sr. Lead Software Engineer - Back End (Java, Python, AWS)", and
    Exiger's "Tech Lead/Principal Engineer" with its "Database Engineer". The
    normalize stage already separates those, using a text-similarity check that
    a bare key cannot replicate, so matching on content here would undo the
    stage before it.

    The asymmetry decides it. Failing to suppress a repost costs one glance,
    once. Suppressing a distinct posting removes it from every future render,
    silently. So identity is read narrowly, and a posting whose every id has
    changed is treated as new - which is the right answer anyway, because an
    employer who re-listed a role under fresh requisitions is telling you the
    role is still open.
    """
    keys = {f"id:{posting.object_id}"} if posting.object_id else set()
    keys |= {f"id:{value}" for value in posting.alternate_object_ids if value}
    if posting.cluster_key:
        keys.add(f"cluster:{posting.cluster_key}")
    return tuple(sorted(keys))


@dataclass(frozen=True)
class SeenEntry:
    """One posting the pipeline has surfaced."""

    object_id: str
    keys: tuple[str, ...]
    band: str
    """The best band this posting has ever been surfaced at."""
    first_surfaced: str
    last_surfaced: str
    surfaced_count: int = 1
    opened: bool = False
    applied: bool = False
    """A resume went in for *this posting*, per the ``r`` flag on its key."""
    company_applied: bool = False
    """A resume went to this *employer*, per the ``r`` flag on their host.

    Kept separate from ``applied`` rather than folded into it. Browsing a
    company's own listings and applying to something found there sets this and
    not ``applied``, because the role applied to need not be the one that
    surfaced. Merging them would make it impossible to tell whether the screen
    picked the right posting or merely the right company - which is the
    distinction the labels exist to measure."""
    title: str = ""
    company: str = ""
    company_host: str = ""
    """Carried so an employer-level application can be attributed back to the
    posting that surfaced them."""
    """Carried purely so the store can be read and hand-edited without
    cross-referencing another file."""

    def as_record(self) -> JsonDict:
        return {
            "object_id": self.object_id,
            "keys": list(self.keys),
            "band": self.band,
            "first_surfaced": self.first_surfaced,
            "last_surfaced": self.last_surfaced,
            "surfaced_count": self.surfaced_count,
            "opened": self.opened,
            "applied": self.applied,
            "company_applied": self.company_applied,
            "title": self.title,
            "company": self.company,
            "company_host": self.company_host,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> SeenEntry:
        return cls(
            object_id=str(record.get("object_id", "")),
            keys=tuple(record.get("keys", ())),
            band=str(record.get("band", BAND_POSSIBLE)),
            first_surfaced=str(record.get("first_surfaced", "")),
            last_surfaced=str(record.get("last_surfaced", "")),
            surfaced_count=int(record.get("surfaced_count", 1)),
            opened=bool(record.get("opened", False)),
            applied=bool(record.get("applied", False)),
            company_applied=bool(record.get("company_applied", False)),
            title=str(record.get("title", "")),
            company=str(record.get("company", "")),
            company_host=str(record.get("company_host", "")),
        )


class SeenStore:
    """Postings already surfaced, indexed by every key each is known under."""

    def __init__(self, entries: Iterable[SeenEntry] = ()) -> None:
        self._entries: dict[str, SeenEntry] = {}
        self._index: dict[str, str] = {}
        for entry in entries:
            self._put(entry)

    def _put(self, entry: SeenEntry) -> None:
        self._entries[entry.object_id] = entry
        for key in entry.keys:
            self._index[key] = entry.object_id

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[SeenEntry]:
        return list(self._entries.values())

    def lookup(self, keys: Sequence[str]) -> SeenEntry | None:
        """Find a stored entry matching any of these keys."""
        for key in keys:
            owner = self._index.get(key)
            if owner is not None:
                return self._entries[owner]
        return None

    def record(self, posting: Posting, band: str, today: str) -> None:
        """Note that a posting was surfaced, merging with any existing entry."""
        keys = match_keys(posting)
        existing = self.lookup(keys)
        if existing is None:
            self._put(
                SeenEntry(
                    object_id=posting.object_id,
                    keys=keys,
                    band=band,
                    first_surfaced=today,
                    last_surfaced=today,
                    title=posting.raw_title or posting.title,
                    company=posting.company,
                    company_host=posting.company_host or "",
                )
            )
            return
        # Keys accumulate rather than replace: the surviving listing can change
        # between runs, and dropping the old ids would let the posting reappear
        # under an identity the store no longer recognizes.
        merged = replace(
            existing,
            keys=tuple(sorted(set(existing.keys) | set(keys))),
            band=band
            if BAND_RANK.get(band, 0) > BAND_RANK.get(existing.band, 0)
            else existing.band,
            last_surfaced=today,
            surfaced_count=existing.surfaced_count + 1,
        )
        # Re-index under the union, and drop a stale object_id row if the
        # surviving listing changed.
        self._entries.pop(existing.object_id, None)
        self._put(merged)

    def merge_keys(self, posting: Posting) -> None:
        """Teach the store a suppressed posting's current keys.

        Suppression alone would let identity drift outrun the store: if run one
        knows a posting as ids {a, b}, run two as {b, c}, and run three as
        {c, d}, then a store that only learns from surfaced postings still
        holds {a, b} on run three and the posting reappears as new. Merging on
        every match keeps the union growing.

        Deliberately does not move ``last_surfaced`` or increment the count -
        the posting was seen in the scrape, not shown to anyone.
        """
        keys = match_keys(posting)
        existing = self.lookup(keys)
        if existing is None:
            return
        merged = replace(existing, keys=tuple(sorted(set(existing.keys) | set(keys))))
        self._entries.pop(existing.object_id, None)
        self._put(merged)

    def apply_visit_log(
        self, opened: Mapping[str, bool], applied_hosts: frozenset[str] = frozenset()
    ) -> tuple[int, int, int]:
        """Attach `opened`, `applied` and `company_applied` from a VisitLogger export.

        Posting keys are checked against every id an entry is known under,
        because the surviving listing - and so the key the render wrote - can
        change between runs. Employer keys are matched by host, subdomains
        included, since the log records whichever host was actually opened.

        Returns the counts now marked.
        """
        for object_id, entry in list(self._entries.items()):
            candidates = [entry.object_id, *(k[3:] for k in entry.keys if k.startswith("id:"))]
            marks = [opened[posting_key(c)] for c in candidates if posting_key(c) in opened]
            company = applied_to(entry.company_host, applied_hosts)
            if not marks and not company:
                continue
            self._entries[object_id] = replace(
                entry,
                opened=entry.opened or bool(marks),
                applied=entry.applied or any(marks),
                company_applied=entry.company_applied or company,
            )
        return (
            sum(1 for e in self._entries.values() if e.opened),
            sum(1 for e in self._entries.values() if e.applied),
            sum(1 for e in self._entries.values() if e.company_applied),
        )


def load_seen_store(path: Path) -> SeenStore:
    """Read the store, treating a missing file as an empty one."""
    if not path.exists():
        return SeenStore()
    return SeenStore(SeenEntry.from_record(record) for record in read_jsonl(path))


def save_seen_store(store: SeenStore, path: Path) -> None:
    """Write the store atomically.

    Rewritten rather than appended so there is exactly one line per posting,
    which keeps it greppable and hand-editable. The write goes to a temporary
    file and is renamed into place: this is the one artifact whose loss is both
    silent and expensive, so a crash must leave the previous version intact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    with JsonlWriter(temp) as writer:
        for entry in sorted(store.entries(), key=lambda e: (e.first_surfaced, e.company)):
            writer.write(entry.as_record())
    os.replace(temp, path)


def load_opened_postings(path: Path) -> dict[str, bool]:
    """Read posting keys from a VisitLogger export, mapped to their applied flag.

    Keys are taken verbatim. Host normalization lower-cases, and posting ids are
    case-sensitive, so normalizing here would silently break every lookup.

    A missing export is not an error. The labels are for later analysis and
    suppression does not depend on them, so an absent or not-yet-exported file
    should cost a warning rather than a failed run in the middle of a chain.
    """
    if not path.exists():
        logger.warning("%s: visit log not found; no opened/applied labels recorded", path)
        return {}
    parsed: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: expected a JSON object")
    opened: dict[str, bool] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not key.startswith("job:"):
            continue
        opened[key] = isinstance(value, Mapping) and value.get("r") is True
    return opened


def load_applied_hosts(path: Path) -> frozenset[str]:
    """Employer hosts flagged with ``r`` in a VisitLogger export.

    Read here rather than through company discovery's loader, which normalizes
    every key and would discard the `job:` entries as unusable. Keys are taken
    as hosts and matched by domain later, since the log records whichever host
    was opened - often a careers subdomain.
    """
    if not path.exists():
        return frozenset()
    parsed: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return frozenset()
    return frozenset(
        key
        for key, value in parsed.items()
        if isinstance(key, str)
        and not key.startswith(POSTING_KEY_PREFIX)
        and isinstance(value, Mapping)
        and value.get("r") is True
    )


@dataclass(frozen=True)
class DiffResult:
    shortlist_path: Path
    meta_path: Path
    store_path: Path
    surfaced: int = 0
    suppressed: int = 0
    promoted: int = 0
    new_postings: int = 0
    store_size: int = 0
    opened: int = 0
    applied: int = 0
    company_applied: int = 0
    bands: dict[str, int] = field(default_factory=dict)


def run_diff(
    records: Iterable[Any],
    out_dir: Path,
    store_path: Path,
    *,
    visit_log_path: Path | None = None,
    source_path: Path | None = None,
    compress: bool = True,
    dry_run: bool = False,
) -> DiffResult:
    """Suppress already-surfaced postings and update the store."""
    from collections import Counter

    today = datetime.now().strftime("%Y-%m-%d")
    store = load_seen_store(store_path)
    store_size_before = len(store)

    opened_count = applied_count = company_applied_count = 0
    if visit_log_path is not None:
        opened_count, applied_count, company_applied_count = store.apply_visit_log(
            load_opened_postings(visit_log_path), load_applied_hosts(visit_log_path)
        )

    surfaced: list[Verdict] = []
    suppressed_postings: list[Posting] = []
    promoted = new_postings = 0
    for record in records:
        band = str(record.get("band", ""))
        if band not in SURFACED_BANDS:
            continue
        posting = Posting.from_record(
            {
                k: v
                for k, v in record.items()
                if k not in ("band", "reject_reasons", "demote_reasons")
            }
        )
        existing = store.lookup(match_keys(posting))
        if existing is None:
            new_postings += 1
        elif BAND_RANK.get(band, 0) > BAND_RANK.get(existing.band, 0):
            promoted += 1
        else:
            suppressed_postings.append(posting)
            continue
        surfaced.append(Verdict(posting, band, (), tuple(record.get("demote_reasons", ()))))

    if not dry_run:
        for verdict in surfaced:
            store.record(verdict.posting, verdict.band, today)
        for posting in suppressed_postings:
            store.merge_keys(posting)
        save_seen_store(store, store_path)

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    shortlist_path = unique_path(with_compression(out_dir / f"shortlist-{stamp}.jsonl", compress))
    meta_path = unique_path(out_dir / f"diff-meta-{stamp}.json")

    order = {BAND_STRONG: 0, BAND_POSSIBLE: 1}
    surfaced.sort(key=lambda v: (order[v.band], -(v.posting.comp_max or 0), v.posting.company))
    with JsonlWriter(shortlist_path) as writer:
        for verdict in surfaced:
            writer.write(verdict.as_record())

    bands: Counter[str] = Counter(v.band for v in surfaced)
    meta: JsonDict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": str(source_path) if source_path else None,
        "shortlist_file": shortlist_path.name,
        "store": str(store_path),
        "visit_log": str(visit_log_path) if visit_log_path else None,
        "dry_run": dry_run,
        "surfaced": len(surfaced),
        "of_which_new": new_postings,
        "of_which_promoted": promoted,
        "suppressed_as_already_seen": len(suppressed_postings),
        "bands": dict(bands),
        "store_entries_before": store_size_before,
        "store_entries_after": len(store),
        "opened": opened_count,
        "applied": applied_count,
        "company_applied": company_applied_count,
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(
        "surfaced %d (%d new, %d promoted), suppressed %d, store now %d",
        len(surfaced),
        new_postings,
        promoted,
        len(suppressed_postings),
        len(store),
    )
    return DiffResult(
        shortlist_path=shortlist_path,
        meta_path=meta_path,
        store_path=store_path,
        surfaced=len(surfaced),
        suppressed=len(suppressed_postings),
        promoted=promoted,
        new_postings=new_postings,
        store_size=len(store),
        opened=opened_count,
        applied=applied_count,
        company_applied=company_applied_count,
        bands=dict(bands),
    )
