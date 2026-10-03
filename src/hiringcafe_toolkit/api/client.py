"""HTTP client for hiring.cafe's search API.

hiring.cafe has no documented public API. This client makes the same requests
the site's own frontend makes:

  1. GET https://hiringcafe.com/?searchState=<json>
     HTML whose ``__NEXT_DATA__`` script tag carries the current Next.js build
     id along with the first page of results (server-side rendered).
  2. GET https://hiringcafe.com/_next/data/<build_id>/<route>.json
         ?searchState=<json>&page=<n>
     JSON for subsequent pages.

The build id changes whenever the site is redeployed, so it is read at run
time and refreshed transparently if it goes stale mid-run.

The route is read at run time too. The search page moved from ``/`` to
``/classic`` (around 2026-09-30): the landing request is redirected there, but
the old ``index.json`` data route answers every page with a redirect payload
and no records, which reads exactly like the end of the result set. So the
route is taken from the page the landing request ended up on, and a redirect
payload is treated as a stale build rather than as an empty page.

The client is deliberately thin: it fetches and parses, but takes no view on
deduplication, output format, or when a result set is "complete". Those are
pipeline decisions.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any
from urllib.parse import urlencode

import httpx

JsonDict = dict[str, Any]

BASE_URL = "https://hiringcafe.com"
NEXT_DATA_OPEN = '<script id="__NEXT_DATA__" type="application/json">'
NEXT_DATA_CLOSE = "</script>"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5.0
BUILD_ID_REFRESH_LIMIT = 3

SSR_PAGE_LABEL = "ssr"

logger = logging.getLogger(__name__)


class HiringCafeError(Exception):
    """Base class for errors raised by this client."""


class StaleBuildIdError(HiringCafeError):
    """The data route rejected the build id; the site was redeployed."""


class ResponseParseError(HiringCafeError):
    """A response arrived but did not have the expected shape."""


@dataclass(frozen=True)
class ResultPage:
    """One page of search results."""

    label: str
    """``"ssr"`` for the server-rendered first page, otherwise the page number."""

    records: list[JsonDict]
    reported_totals: dict[str, float] = field(default_factory=dict)
    build_id: str = ""


def compact_json(value: Any) -> str:
    """Serialize without whitespace, the way the site encodes searchState."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def search_url(search_state: Mapping[str, Any], base_url: str = BASE_URL) -> str:
    """The search page URL a person opens to run ``search_state`` by hand.

    Points at ``/classic`` directly rather than ``/``, which redirects there.
    """
    query = urlencode({"searchState": compact_json(search_state)})
    return f"{base_url.rstrip('/')}/classic?{query}"


def extract_next_data(html: str) -> JsonDict:
    """Pull the ``__NEXT_DATA__`` JSON blob out of a rendered page."""
    start = html.find(NEXT_DATA_OPEN)
    if start < 0:
        raise ResponseParseError("__NEXT_DATA__ script tag not found in page HTML")
    start += len(NEXT_DATA_OPEN)
    end = html.find(NEXT_DATA_CLOSE, start)
    if end < 0:
        raise ResponseParseError("__NEXT_DATA__ script tag was not terminated")
    try:
        parsed: Any = json.loads(html[start:end])
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"__NEXT_DATA__ was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ResponseParseError("__NEXT_DATA__ was not a JSON object")
    return parsed


def find_records(payload: Mapping[str, Any]) -> list[JsonDict]:
    """Locate the job records in a ``pageProps``-shaped payload.

    ``pageProps.ssrHits`` is the known location. The fallback scan exists so a
    field rename upstream degrades into a warning rather than silent zero
    results.
    """
    page_props = payload.get("pageProps", payload)
    if not isinstance(page_props, Mapping):
        raise ResponseParseError("payload had no usable pageProps object")

    hits = page_props.get("ssrHits")
    if isinstance(hits, list):
        return [item for item in hits if isinstance(item, dict)]

    for key, value in page_props.items():
        if (
            isinstance(value, list)
            and value
            and isinstance(value[0], dict)
            and ("objectID" in value[0] or "id" in value[0])
        ):
            logger.warning("ssrHits not found; falling back to field %r", key)
            return [item for item in value if isinstance(item, dict)]
    return []


def find_reported_totals(payload: Mapping[str, Any]) -> dict[str, float]:
    """Collect scalar fields that look like result totals (e.g. ssrTotalCount)."""
    page_props = payload.get("pageProps", payload)
    if not isinstance(page_props, Mapping):
        return {}
    totals: dict[str, float] = {}
    for key, value in page_props.items():
        lowered = key.lower()
        looks_like_total = lowered.startswith("nb") or any(
            token in lowered for token in ("total", "count", "numhits", "numpages")
        )
        if looks_like_total and isinstance(value, (int, float)) and not isinstance(value, bool):
            totals[key] = value
    return totals


def record_key(record: Mapping[str, Any]) -> str | None:
    """Stable identity for a record, used for cross-page deduplication."""
    key = record.get("objectID") or record.get("id")
    return str(key) if key is not None else None


class HiringCafeClient:
    """Fetches search result pages from hiring.cafe."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        delay_seconds: float = 1.0,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.Client | None = None,
    ) -> None:
        # Resolved at construction, not as a default argument value, so the
        # module-level BASE_URL can be pointed elsewhere for local testing.
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.delay_seconds = delay_seconds
        self.retries = retries
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,  # httpx does not follow redirects by default
            headers={
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": self.base_url + "/",
            },
        )

    def __enter__(self) -> HiringCafeClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ----- transport ------------------------------------------------------

    def _get(
        self, url: str, params: Mapping[str, Any], headers: Mapping[str, str] | None = None
    ) -> httpx.Response:
        """GET with retries on transport errors and 5xx responses."""
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self._client.get(url, params=dict(params), headers=dict(headers or {}))
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code < 500:
                    return response
                last_error = HiringCafeError(f"HTTP {response.status_code}")
            if attempt < self.retries:
                wait = RETRY_BACKOFF_SECONDS * attempt
                logger.warning(
                    "transient error (%s); retrying in %.0fs (attempt %d/%d)",
                    last_error,
                    wait,
                    attempt,
                    self.retries,
                )
                time.sleep(wait)
        raise HiringCafeError(f"request failed after {self.retries} attempts: {last_error}")

    def fetch_landing_page(self, search_state_json: str) -> JsonDict:
        """Fetch the search page and return its parsed ``__NEXT_DATA__`` blob."""
        response = self._get(self.base_url + "/", {"searchState": search_state_json})
        if response.status_code != 200:
            raise HiringCafeError(f"landing page returned HTTP {response.status_code}")
        return extract_next_data(response.text)

    def fetch_data_page(
        self, build_id: str, search_state_json: str, page: int, route: str = "index"
    ) -> JsonDict:
        """Fetch one page from the Next.js data route."""
        url = f"{self.base_url}/_next/data/{build_id}/{route}.json"
        response = self._get(
            url,
            {"searchState": search_state_json, "page": page},
            {"x-nextjs-data": "1"},
        )
        if response.status_code == 404:
            raise StaleBuildIdError(f"build id {build_id} rejected for page {page}")
        if response.status_code != 200:
            raise HiringCafeError(
                f"data route returned HTTP {response.status_code} for page {page}"
            )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            # HTML where JSON was expected is the other stale-build symptom.
            raise StaleBuildIdError(f"non-JSON response for page {page}") from exc
        if not isinstance(payload, dict):
            raise ResponseParseError(f"page {page} payload was not a JSON object")
        page_props = payload.get("pageProps")
        if isinstance(page_props, dict) and "__N_REDIRECT" in page_props:
            # Next.js answers a moved page with a redirect payload and no
            # records. Read as a page it would end the run as if exhausted.
            raise StaleBuildIdError(
                f"data route {route} redirected to {page_props['__N_REDIRECT']!r} for page {page}"
            )
        return payload

    # ----- iteration ------------------------------------------------------

    @staticmethod
    def _data_route(next_data: Mapping[str, Any]) -> str:
        """The data route matching the page that rendered ``next_data``.

        ``__NEXT_DATA__`` names its page: ``/`` serves ``index.json``,
        ``/classic`` serves ``classic.json``.
        """
        page = next_data.get("page")
        if not isinstance(page, str) or page.strip("/") == "":
            return "index"
        return page.strip("/")

    def iter_pages(self, search_state: Mapping[str, Any], max_pages: int) -> Iterator[ResultPage]:
        """Yield result pages, starting with the server-rendered one.

        Iteration stops after an empty page or once ``max_pages`` data pages
        have been fetched. Callers may stop earlier; deciding when a result set
        is exhausted is a pipeline concern, not a transport one.
        """
        search_state_json = compact_json(search_state)

        next_data = self.fetch_landing_page(search_state_json)
        build_id = next_data.get("buildId")
        if not isinstance(build_id, str) or not build_id:
            raise ResponseParseError("buildId not found in __NEXT_DATA__")
        route = self._data_route(next_data)
        logger.info("build id: %s, data route: %s", build_id, route)

        props = next_data.get("props", {})
        if not isinstance(props, dict):
            raise ResponseParseError("__NEXT_DATA__ had no props object")

        ssr_records = find_records(props)
        yield ResultPage(
            label=SSR_PAGE_LABEL,
            records=ssr_records,
            reported_totals=find_reported_totals(props),
            build_id=build_id,
        )
        if not ssr_records:
            return

        page = 1
        refreshes = 0
        while page <= max_pages:
            time.sleep(self.delay_seconds)
            try:
                payload = self.fetch_data_page(build_id, search_state_json, page, route)
            except StaleBuildIdError:
                refreshes += 1
                if refreshes > BUILD_ID_REFRESH_LIMIT:
                    raise HiringCafeError(
                        f"build id went stale {refreshes} times; giving up"
                    ) from None
                logger.warning("build id stale; refreshing")
                next_data = self.fetch_landing_page(search_state_json)
                refreshed = next_data.get("buildId")
                if isinstance(refreshed, str) and refreshed:
                    build_id = refreshed
                route = self._data_route(next_data)
                continue  # retry the same page with the fresh build id

            records = find_records(payload)
            yield ResultPage(
                label=str(page),
                records=records,
                reported_totals=find_reported_totals(payload),
                build_id=build_id,
            )
            if not records:
                return
            page += 1
