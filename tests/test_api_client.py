"""Tests for the hiring.cafe API client.

Requests are served by an httpx MockTransport, so these exercise the real
client code paths (params, headers, status handling) without network access.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from hiringcafe_toolkit.api.client import (
    BlockedError,
    HiringCafeClient,
    HiringCafeError,
    ResponseParseError,
    StaleBuildIdError,
    compact_json,
    extract_next_data,
    find_records,
    find_reported_totals,
    record_key,
)

JsonDict = dict[str, Any]


def landing_html(build_id: str, page_props: JsonDict, page: str | None = None) -> str:
    payload: JsonDict = {"buildId": build_id, "props": {"pageProps": page_props}}
    if page is not None:
        payload["page"] = page
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></body></html>"
    )


def make_client(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any
) -> HiringCafeClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, follow_redirects=True)
    return HiringCafeClient(delay_seconds=0.0, client=http_client, **kwargs)


def records(*ids: str) -> list[JsonDict]:
    return [{"objectID": object_id} for object_id in ids]


# ----- parsing helpers ---------------------------------------------------


def test_extract_next_data_reads_build_id() -> None:
    html = landing_html("build-1", {"ssrHits": []})
    assert extract_next_data(html)["buildId"] == "build-1"


def test_extract_next_data_rejects_missing_tag() -> None:
    with pytest.raises(ResponseParseError):
        extract_next_data("<html><body>no next data here</body></html>")


def test_find_records_prefers_ssr_hits() -> None:
    payload = {"pageProps": {"ssrHits": records("a", "b"), "other": [{"objectID": "z"}]}}
    assert [r["objectID"] for r in find_records(payload)] == ["a", "b"]


def test_find_records_falls_back_when_field_renamed() -> None:
    payload = {"pageProps": {"renamedHits": records("a")}}
    assert [r["objectID"] for r in find_records(payload)] == ["a"]


def test_find_reported_totals_picks_up_count_fields() -> None:
    payload = {"pageProps": {"ssrTotalCount": 17775, "ssrCompanyCount": 2846, "query": "x"}}
    assert find_reported_totals(payload) == {"ssrTotalCount": 17775, "ssrCompanyCount": 2846}


def test_find_reported_totals_ignores_booleans() -> None:
    assert find_reported_totals({"pageProps": {"hasCount": True}}) == {}


def test_record_key_falls_back_to_id() -> None:
    assert record_key({"id": "x"}) == "x"
    assert record_key({}) is None


def test_compact_json_has_no_whitespace() -> None:
    assert compact_json({"a": [1, 2]}) == '{"a":[1,2]}'


# ----- iteration ---------------------------------------------------------


def test_iter_pages_walks_ssr_then_data_route() -> None:
    pages: dict[int, list[JsonDict]] = {1: records("c", "d"), 2: records("e"), 3: []}
    seen_params: list[JsonDict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        if "_next/data" in request.url.path:
            seen_params.append({k: v[0] for k, v in query.items()})
            page = int(query["page"][0])
            return httpx.Response(200, json={"pageProps": {"ssrHits": pages[page]}})
        return httpx.Response(
            200,
            html=landing_html("build-1", {"ssrHits": records("a", "b"), "ssrTotalCount": 5}),
        )

    with make_client(handler) as api:
        result = list(api.iter_pages({"q": 1}, max_pages=10))

    assert [p.label for p in result] == ["ssr", "1", "2", "3"]
    assert [len(p.records) for p in result] == [2, 2, 1, 0]
    assert result[0].reported_totals == {"ssrTotalCount": 5}
    assert all(p.build_id == "build-1" for p in result)
    # searchState is forwarded verbatim on every data-route request.
    assert {p["searchState"] for p in seen_params} == {'{"q":1}'}


def test_iter_pages_stops_at_max_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "_next/data" in request.url.path:
            return httpx.Response(200, json={"pageProps": {"ssrHits": records("x")}})
        return httpx.Response(200, html=landing_html("b", {"ssrHits": records("a")}))

    with make_client(handler) as api:
        labels = [p.label for p in api.iter_pages({}, max_pages=3)]

    assert labels == ["ssr", "1", "2", "3"]


def test_iter_pages_skips_data_route_when_ssr_is_empty() -> None:
    calls = {"data": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "_next/data" in request.url.path:
            calls["data"] += 1
            return httpx.Response(200, json={"pageProps": {"ssrHits": []}})
        return httpx.Response(200, html=landing_html("b", {"ssrHits": []}))

    with make_client(handler) as api:
        labels = [p.label for p in api.iter_pages({}, max_pages=5)]

    assert labels == ["ssr"]
    assert calls["data"] == 0


def test_data_route_follows_the_page_the_landing_request_rendered() -> None:
    """The search page moved from / to /classic; index.json then only redirects."""
    data_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "_next/data" in request.url.path:
            data_paths.append(request.url.path)
            return httpx.Response(200, json={"pageProps": {"ssrHits": []}})
        return httpx.Response(
            200, html=landing_html("b", {"ssrHits": records("a")}, page="/classic")
        )

    with make_client(handler) as api:
        list(api.iter_pages({}, max_pages=5))

    assert data_paths == ["/_next/data/b/classic.json"]


def test_a_root_page_uses_the_index_route() -> None:
    data_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "_next/data" in request.url.path:
            data_paths.append(request.url.path)
            return httpx.Response(200, json={"pageProps": {"ssrHits": []}})
        return httpx.Response(200, html=landing_html("b", {"ssrHits": records("a")}, page="/"))

    with make_client(handler) as api:
        list(api.iter_pages({}, max_pages=5))

    assert data_paths == ["/_next/data/b/index.json"]


def test_a_redirect_payload_is_never_read_as_an_empty_page() -> None:
    """Reading it as a page would end the run early and report it as finished."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "_next/data" in request.url.path:
            redirect = {"__N_REDIRECT": "/classic?searchState=x", "__N_REDIRECT_STATUS": 307}
            return httpx.Response(200, json={"pageProps": redirect, "__N_SSP": True})
        return httpx.Response(200, html=landing_html("b", {"ssrHits": records("a")}))

    with make_client(handler) as api, pytest.raises(HiringCafeError, match="went stale"):
        list(api.iter_pages({}, max_pages=5))


def test_a_cloudflare_challenge_is_reported_as_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, headers={"cf-mitigated": "challenge"}, html="<title>Just a moment...</title>"
        )

    with make_client(handler) as api, pytest.raises(BlockedError, match="Cloudflare challenge"):
        api.fetch_landing_page("{}")


def test_a_challenge_mid_run_is_reported_as_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "_next/data" in request.url.path:
            return httpx.Response(403, headers={"cf-mitigated": "challenge"})
        return httpx.Response(200, html=landing_html("b", {"ssrHits": records("a")}))

    with make_client(handler) as api, pytest.raises(BlockedError):
        list(api.iter_pages({}, max_pages=5))


def test_a_plain_403_is_not_reported_as_a_challenge() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with make_client(handler) as api, pytest.raises(HiringCafeError) as raised:
        api.fetch_landing_page("{}")

    assert not isinstance(raised.value, BlockedError)


# ----- resilience --------------------------------------------------------


def test_stale_build_id_is_refreshed_and_page_retried() -> None:
    state = {"landing": 0, "data": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "_next/data" in request.url.path:
            state["data"] += 1
            if state["data"] == 1:
                return httpx.Response(404, text="not found")
            assert "build-2" in request.url.path
            return httpx.Response(200, json={"pageProps": {"ssrHits": records("c")}})
        state["landing"] += 1
        build = "build-1" if state["landing"] == 1 else "build-2"
        return httpx.Response(200, html=landing_html(build, {"ssrHits": records("a")}))

    with make_client(handler) as api:
        pages = list(api.iter_pages({}, max_pages=1))

    assert state["landing"] == 2
    assert [p.label for p in pages] == ["ssr", "1"]
    assert pages[-1].build_id == "build-2"


def test_html_instead_of_json_is_treated_as_stale_build() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html>login wall</html>")

    with make_client(handler) as api, pytest.raises(StaleBuildIdError):
        api.fetch_data_page("build-1", "{}", 1)


def test_repeated_stale_build_gives_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "_next/data" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, html=landing_html("b", {"ssrHits": records("a")}))

    with make_client(handler) as api, pytest.raises(HiringCafeError, match="went stale"):
        list(api.iter_pages({}, max_pages=5))


def test_server_errors_are_retried_then_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hiringcafe_toolkit.api.client.time.sleep", lambda _seconds: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503)

    with make_client(handler, retries=3) as api, pytest.raises(HiringCafeError, match="after 3"):
        api.fetch_landing_page("{}")

    assert attempts["n"] == 3


def test_transient_transport_error_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hiringcafe_toolkit.api.client.time.sleep", lambda _seconds: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, html=landing_html("b", {"ssrHits": []}))

    with make_client(handler) as api:
        assert api.fetch_landing_page("{}")["buildId"] == "b"

    assert attempts["n"] == 2


def test_client_follows_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(302, headers={"Location": "/search"})
        return httpx.Response(200, html=landing_html("b", {"ssrHits": []}))

    with make_client(handler) as api:
        assert api.fetch_landing_page("{}")["buildId"] == "b"
