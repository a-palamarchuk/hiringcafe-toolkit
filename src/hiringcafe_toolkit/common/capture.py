"""Read result pages saved by the browser capture extension.

hiring.cafe answers automated clients with a Cloudflare challenge some days.
On those days the search is run by hand in Firefox, and the extension in
``extensions/hiringcafe-capture/`` saves each result page the browser receives.
It only watches: paging is done by the person at the keyboard.

A capture file is a flat list of pages, each tagged with the searchState and
page number taken from the URL it arrived on::

    {"format": "hiringcafe-capture/1", "saved_at": "...", "pages": [
        {"url": "...", "searchState": {...}, "page": 0, "source": "ssr",
         "build_id": "...", "captured_at": "...", "payload": {"pageProps": {...}}}
    ], "ends": [
        {"searchState": {...}, "page": 41, "reason": "no next page link"}
    ]}

``payload`` is kept whole - ``__NEXT_DATA__.props`` for a page loaded as HTML,
the JSON body for one fetched by "next page" - so that finding the records in
it stays in ``api.find_records``, the same code the live client uses, rather
than being reimplemented in the extension.

``ends`` records where the extension saw a search's "next page" link
disappear, which is how a capture knows it reached the last page. Older
captures lack it.

A capture may differ from its configured search in the fetch window alone:
daily captures use a short one, so paging by hand covers only what arrived
since the last run.

This module groups the pages into one ``CapturedSearch`` per searchState and
serves them through the scrape stage's ``PageSource`` protocol, so an import
writes exactly what a live scrape would.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.api import (
    SSR_PAGE_LABEL,
    ResultPage,
    compact_json,
    find_records,
    find_reported_totals,
)

JsonDict = dict[str, Any]

CAPTURE_FORMAT = "hiringcafe-capture/1"

#: The one searchState field a capture may differ in from its configured
#: search: daily captures use a short fetch window to keep paging by hand short.
WINDOW_FIELD = "dateFetchedPastNDays"


class CaptureError(Exception):
    """A capture file is missing, malformed, or cannot serve a search."""


@dataclass(frozen=True)
class CapturedPage:
    number: int
    """0 for the first page, then the ``page`` URL parameter."""
    payload: JsonDict
    captured_at: str
    build_id: str = ""

    @property
    def records(self) -> list[JsonDict]:
        return find_records(self.payload)


def _state_key(state: Mapping[str, Any]) -> str:
    """Exact identity of a searchState, ignoring key order."""
    return json.dumps(dict(state), sort_keys=True)


def match_key(state: Mapping[str, Any]) -> str:
    """Identity of a search for matching: everything but the fetch window."""
    return _state_key({k: v for k, v in state.items() if k != WINDOW_FIELD})


@dataclass(frozen=True)
class CapturedSearch:
    """Every captured page of one searchState, one per page number."""

    search_state: JsonDict
    pages: tuple[CapturedPage, ...]
    """Sorted by page number."""
    end: JsonDict | None = None
    """Where the extension saw the results end (``page``, ``reason``), if it did."""

    @property
    def window_days(self) -> int | None:
        window = self.search_state.get(WINDOW_FIELD)
        return window if isinstance(window, int) and not isinstance(window, bool) else None

    @property
    def complete(self) -> bool:
        """Whether the last page of results was captured.

        Either the extension saw the "next page" link disappear, or a captured
        page came back empty - the same end signal the live scraper uses.
        """
        if not self.pages:
            return False
        if not self.pages[-1].records:
            return True
        # Strict equality: an end page below the last captured page means the
        # end was recorded wrongly, and is no evidence the end was reached.
        return self.end is not None and self.pages[-1].number == self.end["page"]

    @property
    def page_numbers(self) -> list[int]:
        return [page.number for page in self.pages]

    @property
    def missing_pages(self) -> list[int]:
        """Page numbers skipped between the first page and the last captured.

        A gap means a click was missed, or a page came from the browser's cache
        rather than the network and so was never seen by the extension.
        """
        present = set(self.page_numbers)
        return [n for n in range(max(present, default=-1) + 1) if n not in present]

    @property
    def last_page_records(self) -> int:
        """Records on the highest page. A short last page means the end was reached."""
        return len(self.pages[-1].records) if self.pages else 0

    @property
    def first_captured(self) -> datetime | None:
        """When the earliest page was captured, if its stamp is readable."""
        stamps = [_parse_time(page.captured_at) for page in self.pages]
        known = [stamp for stamp in stamps if stamp is not None]
        return min(known, default=None)

    @property
    def capture_span_hours(self) -> float:
        """Hours between the earliest and latest captured page.

        A long span usually means pages from an earlier session were not
        cleared, and old pages are mixed in with today's.
        """
        stamps = [_parse_time(page.captured_at) for page in self.pages]
        known = [stamp for stamp in stamps if stamp is not None]
        if len(known) < 2:
            return 0.0
        return (max(known) - min(known)).total_seconds() / 3600

    def describe(self) -> str:
        """Short, recognizable summary of the searchState for messages."""
        return compact_json(self.search_state)[:160]

    def summary(self) -> JsonDict:
        """What the import meta records about this capture."""
        return {
            "window_days": self.window_days,
            "complete": self.complete,
            "end": self.end,
            "pages": self.page_numbers,
            "missing_pages": self.missing_pages,
            "last_page_records": self.last_page_records,
            "first_captured_at": min((p.captured_at for p in self.pages), default=None),
            "last_captured_at": max((p.captured_at for p in self.pages), default=None),
        }


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _page_entry(entry: Any, index: int, path: Path) -> tuple[JsonDict, CapturedPage]:
    where = f"{path}: pages[{index}]"
    if not isinstance(entry, dict):
        raise CaptureError(f"{where} is not an object")
    state = entry.get("searchState")
    if not isinstance(state, dict) or not state:
        raise CaptureError(f"{where} has no searchState object")
    number = entry.get("page")
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        raise CaptureError(f"{where} has no valid page number")
    payload = entry.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("pageProps"), dict):
        raise CaptureError(f"{where} has no pageProps payload")
    if "__N_REDIRECT" in payload["pageProps"]:
        # The extension drops these; one arriving here means it did not, and
        # read as a page it would end the search early with no records.
        raise CaptureError(f"{where} is a redirect, not a result page")
    captured_at = entry.get("captured_at")
    build_id = entry.get("build_id")
    page = CapturedPage(
        number=number,
        payload=payload,
        captured_at=captured_at if isinstance(captured_at, str) else "",
        build_id=build_id if isinstance(build_id, str) else "",
    )
    return state, page


def load_capture(path: Path) -> list[CapturedSearch]:
    """Read a capture file and group its pages by searchState.

    Where one page number was captured more than once - a page revisited, or
    reloaded - the latest capture wins.
    """
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CaptureError(f"{path}: capture file not found") from exc
    except json.JSONDecodeError as exc:
        raise CaptureError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict) or loaded.get("format") != CAPTURE_FORMAT:
        raise CaptureError(f"{path}: not a {CAPTURE_FORMAT} file")
    entries = loaded.get("pages")
    if not isinstance(entries, list):
        raise CaptureError(f"{path}: pages must be a list")

    states: dict[str, JsonDict] = {}
    pages: dict[str, dict[int, CapturedPage]] = {}
    for index, entry in enumerate(entries):
        state, page = _page_entry(entry, index, path)
        # Key order in the URL is the site's business; equality is not.
        key = _state_key(state)
        states.setdefault(key, state)
        by_number = pages.setdefault(key, {})
        current = by_number.get(page.number)
        if current is None or page.captured_at >= current.captured_at:
            by_number[page.number] = page

    ends = _ends(loaded.get("ends"), path)
    return [
        CapturedSearch(
            search_state=states[key],
            pages=tuple(by_number[n] for n in sorted(by_number)),
            end=ends.get(key),
        )
        for key, by_number in pages.items()
    ]


def _ends(raw: Any, path: Path) -> dict[str, JsonDict]:
    """Where the extension saw each search end. Absent in older captures."""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise CaptureError(f"{path}: ends must be a list")
    ends: dict[str, JsonDict] = {}
    for index, item in enumerate(raw):
        state = item.get("searchState") if isinstance(item, dict) else None
        page = item.get("page") if isinstance(item, dict) else None
        if not isinstance(state, dict) or isinstance(page, bool) or not isinstance(page, int):
            raise CaptureError(f"{path}: ends[{index}] needs a searchState and a page number")
        ends[_state_key(state)] = {"page": page, "reason": str(item.get("reason", ""))}
    return ends


def match_captures(
    configured: Sequence[Mapping[str, Any]], captured: Sequence[CapturedSearch]
) -> tuple[list[CapturedSearch | None], list[CapturedSearch]]:
    """Pair each configured searchState with its capture.

    Returns the capture for each configured search in order (``None`` where
    there is none), and the captures used for none of them. A capture matches
    when its searchState is identical but for the fetch window: a capture of a
    near-identical search would be imported as if it were the configured one.
    Where a search was captured with two windows, the wider one is used, since
    it covers the other.
    """
    candidates: dict[str, list[CapturedSearch]] = {}
    for capture in captured:
        candidates.setdefault(match_key(capture.search_state), []).append(capture)

    matched: list[CapturedSearch | None] = []
    used: set[int] = set()
    for state in configured:
        options = candidates.get(match_key(state), [])
        best = max(options, key=lambda c: c.window_days or 0, default=None)
        matched.append(best)
        if best is not None:
            used.add(id(best))
    unmatched = [capture for capture in captured if id(capture) not in used]
    return matched, unmatched


class CapturePageSource:
    """Serves captured pages to the scrape stage in place of the live client."""

    def __init__(self, captures: Sequence[CapturedSearch]) -> None:
        """``captures`` are the ones ``match_captures`` chose, at most one per search."""
        self._by_key = {match_key(c.search_state): c for c in captures}

    def iter_pages(self, search_state: Mapping[str, Any], max_pages: int) -> Iterator[ResultPage]:
        capture = self._by_key.get(match_key(search_state))
        if capture is None:
            raise CaptureError(f"no captured pages for searchState {compact_json(search_state)}")
        for page in capture.pages:
            if page.number > max_pages:
                return
            yield ResultPage(
                label=SSR_PAGE_LABEL if page.number == 0 else str(page.number),
                records=page.records,
                reported_totals=find_reported_totals(page.payload),
                build_id=page.build_id,
            )
