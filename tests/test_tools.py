"""Tests for the three MCP tools.

``@mcp.tool()`` registers the function and returns it unchanged, so the tools
are called here as plain functions rather than through ``mcp.call_tool``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import respx
from mcp.server.mcpserver import Context

from parcel_mcp import client
from parcel_mcp.client import DELIVERIES_TTL, ParcelError
from parcel_mcp.server import (
    add_delivery,
    deliveries_resource,
    list_deliveries,
    resolve_api_key,
    search_carriers,
)

from .conftest import (
    ADD_DELIVERY_URL,
    DELIVERIES_URL,
    FAKE_TOKEN,
    OTHER_TOKEN,
    FakeClock,
    MockCarriers,
)


def _deliveries_route(*items: dict[str, Any]) -> respx.Route:
    """Register the deliveries route returning ``items``."""
    return respx.get(DELIVERIES_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "deliveries": list(items)})
    )


def _add_route() -> respx.Route:
    """Register a successful add-delivery route."""
    return respx.post(ADD_DELIVERY_URL).mock(
        return_value=httpx.Response(200, json={"success": True})
    )


# --------------------------------------------------------------------------- #
# list_deliveries
# --------------------------------------------------------------------------- #


@respx.mock
def test_list_deliveries_resolves_status_and_carrier(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    _deliveries_route(delivery)

    result = list_deliveries()

    assert result["count"] == 1
    entry = result["deliveries"][0]
    assert entry["status"] == "in transit"
    assert entry["carrier_name"] == "La Poste"
    # Enrichment adds fields, it never drops the upstream ones.
    assert entry["tracking_number"] == "ABC123"
    assert entry["events"] == delivery["events"]


@respx.mock
def test_list_deliveries_labels_an_unknown_status_code(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    """Label an unrecognised status code instead of failing.

    The table is derived from observation of the live API, so codes outside it
    are expected rather than exceptional.
    """
    mock_carriers()
    _deliveries_route({**delivery, "status_code": 99})

    entry = list_deliveries()["deliveries"][0]

    assert entry["status"] == "unknown status code 99"


@respx.mock
def test_list_deliveries_passes_the_filter_mode(mock_carriers: MockCarriers) -> None:
    mock_carriers()
    route = _deliveries_route()

    list_deliveries("active")

    assert route.calls.last.request.url.params["filter_mode"] == "active"


@respx.mock
def test_list_deliveries_handles_an_empty_account(mock_carriers: MockCarriers) -> None:
    mock_carriers()
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(200, json={"success": True}))

    result = list_deliveries()

    assert result["count"] == 0
    assert result["deliveries"] == []


@respx.mock
def test_list_deliveries_caches_then_refetches_after_the_ttl(
    clock: FakeClock, mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    route = _deliveries_route(delivery)

    list_deliveries()
    list_deliveries()
    assert route.call_count == 1, "second call should be served from the local cache"

    clock.advance(DELIVERIES_TTL + 1)
    list_deliveries()
    assert route.call_count == 2


@respx.mock
def test_list_deliveries_caches_each_filter_mode_separately(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    route = _deliveries_route(delivery)

    list_deliveries("recent")
    list_deliveries("active")

    assert route.call_count == 2


@respx.mock
def test_list_deliveries_propagates_the_rate_limit(mock_carriers: MockCarriers) -> None:
    mock_carriers()
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(429, json={}))

    with pytest.raises(ParcelError, match="rate limit"):
        list_deliveries()


# --------------------------------------------------------------------------- #
# add_delivery: the local guards matter most, since a rejected request still
# counts against the 20-per-day budget upstream.
# --------------------------------------------------------------------------- #


@respx.mock
def test_add_delivery_rejects_an_unknown_carrier_without_spending_a_request(
    mock_carriers: MockCarriers,
) -> None:
    mock_carriers()
    route = _add_route()

    with pytest.raises(ParcelError, match="search_carriers"):
        add_delivery("ABC123", "not-a-carrier", "test")

    assert route.call_count == 0, "a bad carrier code must never reach the network"


@respx.mock
def test_add_delivery_demands_a_postcode_without_spending_a_request(
    mock_carriers: MockCarriers,
) -> None:
    mock_carriers()
    route = _add_route()

    with pytest.raises(ParcelError, match="Bpost requires a postcode"):
        add_delivery("ABC123", "bpost", "test")

    assert route.call_count == 0


@respx.mock
def test_add_delivery_demands_an_email_without_spending_a_request(
    mock_carriers: MockCarriers,
) -> None:
    mock_carriers()
    route = _add_route()

    with pytest.raises(ParcelError, match="Apple Store requires an email"):
        add_delivery("ABC123", "applestore", "test")

    assert route.call_count == 0


@respx.mock
def test_add_delivery_accepts_a_carrier_once_its_extra_field_is_given(
    mock_carriers: MockCarriers,
) -> None:
    mock_carriers()
    route = _add_route()

    add_delivery("ABC123", "bpost", "test", postcode="1000")

    assert route.call_count == 1
    assert route.calls.last.request.read().decode().find("1000") != -1


@respx.mock
def test_add_delivery_normalises_the_submitted_body(mock_carriers: MockCarriers) -> None:
    mock_carriers()
    route = _add_route()

    add_delivery(" ABC123 ", " lp ", " A parcel ", language="FR")

    body = json.loads(route.calls.last.request.read())
    assert body == {
        "tracking_number": "ABC123",
        "carrier_code": "lp",
        "description": "A parcel",
        "language": "fr",
        "send_push_confirmation": False,
    }


@respx.mock
def test_add_delivery_reports_what_it_submitted(mock_carriers: MockCarriers) -> None:
    mock_carriers()
    _add_route()

    result = add_delivery("ABC123", "lp", "A parcel")

    assert result["added"] is True
    assert result["carrier_name"] == "La Poste"
    assert "first update" in result["note"]


@respx.mock
def test_add_delivery_invalidates_the_delivery_cache(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    deliveries = _deliveries_route(delivery)
    _add_route()

    list_deliveries()
    assert deliveries.call_count == 1

    add_delivery("ABC123", "lp", "A parcel")

    list_deliveries()
    assert deliveries.call_count == 2, "the new delivery must not be hidden by a stale cache"


@respx.mock
def test_add_delivery_surfaces_an_upstream_rejection(mock_carriers: MockCarriers) -> None:
    mock_carriers()
    respx.post(ADD_DELIVERY_URL).mock(
        return_value=httpx.Response(
            200, json={"success": False, "error_message": "Invalid tracking number format"}
        )
    )

    with pytest.raises(ParcelError, match="Invalid tracking number format"):
        add_delivery("nonsense", "lp", "test")


# --------------------------------------------------------------------------- #
# search_carriers
# --------------------------------------------------------------------------- #


@respx.mock
def test_search_carriers_matches_on_name(mock_carriers: MockCarriers) -> None:
    mock_carriers()

    result = search_carriers("la poste")

    assert result["total"] == 1
    assert result["carriers"][0] == {"code": "lp", "name": "La Poste"}


@respx.mock
def test_search_carriers_matches_on_code(mock_carriers: MockCarriers) -> None:
    mock_carriers()

    assert search_carriers("pholder")["carriers"][0]["code"] == "pholder"


@respx.mock
def test_search_carriers_matches_on_a_name_variation(mock_carriers: MockCarriers) -> None:
    """Match the alternative names a carrier trades under abroad.

    The catalogue lists them under name_variations, and that is what a user
    searching from another country will type.
    """
    mock_carriers()

    result = search_carriers("chronopost france")

    assert [c["code"] for c in result["carriers"]] == ["chrono"]


@respx.mock
def test_search_carriers_names_the_required_extra_field(mock_carriers: MockCarriers) -> None:
    mock_carriers()

    by_code = {c["code"]: c for c in search_carriers("")["carriers"]}

    assert by_code["bpost"]["extra_required"] == "postcode"
    assert by_code["applestore"]["extra_required"] == "email"
    assert "extra_required" not in by_code["lp"]


@respx.mock
def test_search_carriers_reports_the_total_beyond_the_limit(mock_carriers: MockCarriers) -> None:
    mock_carriers()

    result = search_carriers("", limit=2)

    assert result["count"] == 2
    assert len(result["carriers"]) == 2
    assert result["total"] == 5, "total counts every match, not just the returned page"


@respx.mock
def test_search_carriers_returns_nothing_for_an_unmatched_query(
    mock_carriers: MockCarriers,
) -> None:
    mock_carriers()

    result = search_carriers("no such carrier")

    assert result["total"] == 0
    assert result["carriers"] == []


# --------------------------------------------------------------------------- #
# Where the API key comes from. Over HTTP it belongs to the caller, not to the
# server, so a header must win over the environment.
# --------------------------------------------------------------------------- #


def _ctx(headers: dict[str, str] | None) -> Context:
    """Stand in for the SDK context, carrying only what resolve_api_key reads."""
    return cast("Context", SimpleNamespace(headers=headers))


def test_key_falls_back_to_the_environment_without_a_context() -> None:
    assert resolve_api_key(None) == FAKE_TOKEN


def test_key_falls_back_to_the_environment_on_stdio() -> None:
    """Stdio has no headers at all; the SDK reports None."""
    assert resolve_api_key(_ctx(None)) == FAKE_TOKEN


def test_key_comes_from_the_parcel_token_header() -> None:
    assert resolve_api_key(_ctx({"x-parcel-token": OTHER_TOKEN})) == OTHER_TOKEN


def test_key_comes_from_a_bearer_authorization_header() -> None:
    assert resolve_api_key(_ctx({"authorization": f"Bearer {OTHER_TOKEN}"})) == OTHER_TOKEN


def test_bearer_prefix_is_matched_case_insensitively() -> None:
    assert resolve_api_key(_ctx({"authorization": f"bEaReR {OTHER_TOKEN}"})) == OTHER_TOKEN


def test_header_names_are_matched_case_insensitively() -> None:
    """A plain Mapping is not required to be case-insensitive, unlike starlette's."""
    assert resolve_api_key(_ctx({"X-Parcel-Token": OTHER_TOKEN})) == OTHER_TOKEN


def test_the_header_wins_over_the_environment() -> None:
    assert resolve_api_key(_ctx({"x-parcel-token": OTHER_TOKEN})) != FAKE_TOKEN


def test_a_blank_header_falls_back_to_the_environment() -> None:
    assert resolve_api_key(_ctx({"x-parcel-token": "   "})) == FAKE_TOKEN


def test_a_non_bearer_authorization_is_ignored() -> None:
    """Basic auth is not a Parcel key; forwarding it would spend a request for nothing."""
    assert resolve_api_key(_ctx({"authorization": "Basic dXNlcjpwYXNz"})) == FAKE_TOKEN


def test_no_header_and_no_environment_names_both_ways_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PARCEL_TOKEN")

    with pytest.raises(ParcelError, match="X-Parcel-Token"):
        resolve_api_key(_ctx({}))


# --------------------------------------------------------------------------- #
# One process, several callers: the HTTP case. This is the property that makes
# a shared deployment safe, so it gets its own tests.
# --------------------------------------------------------------------------- #


@respx.mock
def test_two_keys_never_see_each_other_s_deliveries(mock_carriers: MockCarriers) -> None:
    mock_carriers()

    def per_key(request: httpx.Request) -> httpx.Response:
        owner = "mine" if request.headers["api-key"] == FAKE_TOKEN else "theirs"
        return httpx.Response(
            200,
            json={
                "success": True,
                "deliveries": [{"description": owner, "carrier_code": "lp", "status_code": 2}],
            },
        )

    route = respx.get(DELIVERIES_URL).mock(side_effect=per_key)

    mine = list_deliveries(ctx=_ctx({"x-parcel-token": FAKE_TOKEN}))
    theirs = list_deliveries(ctx=_ctx({"x-parcel-token": OTHER_TOKEN}))

    assert mine["deliveries"][0]["description"] == "mine"
    assert theirs["deliveries"][0]["description"] == "theirs"
    assert route.call_count == 2, "the second caller must not be served from the first's cache"


@respx.mock
def test_each_key_gets_its_own_cache(mock_carriers: MockCarriers, delivery: dict[str, Any]) -> None:
    mock_carriers()
    route = _deliveries_route(delivery)

    list_deliveries(ctx=_ctx({"x-parcel-token": FAKE_TOKEN}))
    list_deliveries(ctx=_ctx({"x-parcel-token": FAKE_TOKEN}))
    assert route.call_count == 1

    list_deliveries(ctx=_ctx({"x-parcel-token": OTHER_TOKEN}))
    assert route.call_count == 2


@respx.mock
def test_adding_a_delivery_only_invalidates_its_own_caller(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    route = _deliveries_route(delivery)
    _add_route()

    mine = _ctx({"x-parcel-token": FAKE_TOKEN})
    theirs = _ctx({"x-parcel-token": OTHER_TOKEN})
    list_deliveries(ctx=mine)
    list_deliveries(ctx=theirs)
    assert route.call_count == 2

    add_delivery("ABC123", "lp", "A parcel", ctx=mine)

    list_deliveries(ctx=theirs)
    assert route.call_count == 2, "the other caller's cache must survive"

    list_deliveries(ctx=mine)
    assert route.call_count == 3, "the adding caller's cache must be dropped"


# --------------------------------------------------------------------------- #
# Rate-limit budget and cache reporting
# --------------------------------------------------------------------------- #


@respx.mock
def test_result_reports_a_live_fetch(mock_carriers: MockCarriers, delivery: dict[str, Any]) -> None:
    mock_carriers()
    _deliveries_route(delivery)

    result = list_deliveries()

    assert result["cache"]["served_from_cache"] is False
    assert result["cache"]["age_seconds"] is None
    assert result["rate_limit"]["spent_by_this_server"] == 1
    assert result["rate_limit"]["remaining_at_most"] == 19


@respx.mock
def test_result_reports_a_cache_hit_and_its_age(
    clock: FakeClock, mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    _deliveries_route(delivery)

    list_deliveries()
    clock.advance(30)
    result = list_deliveries()

    assert result["cache"]["served_from_cache"] is True
    assert result["cache"]["age_seconds"] == 30.0


@respx.mock
def test_a_cache_hit_does_not_spend_budget(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    """The whole point of the cache: a repeated call must be free."""
    mock_carriers()
    _deliveries_route(delivery)

    list_deliveries()
    result = list_deliveries()

    assert result["rate_limit"]["spent_by_this_server"] == 1


@respx.mock
def test_a_refused_addition_does_not_spend_budget(mock_carriers: MockCarriers) -> None:
    """A local guard fires before the network, so the daily budget is untouched."""
    mock_carriers()
    _add_route()

    with pytest.raises(ParcelError):
        add_delivery("ABC123", "bpost", "no postcode")

    assert (
        client.budget(client.ADD_BUCKET, client.key_fingerprint(FAKE_TOKEN))["spent_by_this_server"]
        == 0
    )


@respx.mock
def test_add_delivery_reports_the_daily_budget(mock_carriers: MockCarriers) -> None:
    mock_carriers()
    _add_route()

    result = add_delivery("ABC123", "lp", "A parcel")

    assert result["rate_limit"]["per"] == "day"
    assert result["rate_limit"]["spent_by_this_server"] == 1
    assert result["rate_limit"]["remaining_at_most"] == 19


# --------------------------------------------------------------------------- #
# Filtering and sorting, which happen after the fetch and so cost nothing
# --------------------------------------------------------------------------- #


@respx.mock
def test_status_filter_keeps_only_matching_deliveries(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    _deliveries_route(delivery, {**delivery, "description": "done", "status_code": 0})

    result = list_deliveries(status="transit")

    assert result["count"] == 1
    assert result["total_before_filter"] == 2
    assert result["deliveries"][0]["status"] == "in transit"


@respx.mock
def test_status_filter_costs_no_extra_request(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    route = _deliveries_route(delivery)

    list_deliveries()
    list_deliveries(status="transit")

    assert route.call_count == 1


@respx.mock
def test_status_filter_that_matches_nothing_returns_an_empty_list(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    _deliveries_route(delivery)

    result = list_deliveries(status="delivered to a neighbour")

    assert result["count"] == 0
    assert result["total_before_filter"] == 1


@respx.mock
def test_sorting_by_expected_date_puts_undated_deliveries_last(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    """An absent expected date is not an early one, so it must not sort first."""
    mock_carriers()
    _deliveries_route(
        {**delivery, "description": "later", "date_expected": "2026-09-10"},
        {**delivery, "description": "undated", "date_expected": None},
        {**delivery, "description": "sooner", "date_expected": "2026-09-04"},
    )

    result = list_deliveries(sort_by="expected")

    assert [d["description"] for d in result["deliveries"]] == ["sooner", "later", "undated"]


@respx.mock
def test_sorting_by_description_is_case_insensitive(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    _deliveries_route(
        {**delivery, "description": "banana"},
        {**delivery, "description": "Apple"},
    )

    result = list_deliveries(sort_by="description")

    assert [d["description"] for d in result["deliveries"]] == ["Apple", "banana"]


@respx.mock
def test_unsorted_by_default_preserves_the_carrier_order(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    _deliveries_route(
        {**delivery, "description": "zebra"},
        {**delivery, "description": "aardvark"},
    )

    result = list_deliveries()

    assert [d["description"] for d in result["deliveries"]] == ["zebra", "aardvark"]


# --------------------------------------------------------------------------- #
# The parcel://deliveries/{filter_mode} resource
# --------------------------------------------------------------------------- #


@respx.mock
def test_resource_returns_the_deliveries_as_json(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    mock_carriers()
    _deliveries_route(delivery)

    payload = json.loads(deliveries_resource("active"))

    assert payload["filter_mode"] == "active"
    assert payload["count"] == 1
    assert payload["deliveries"][0]["carrier_name"] == "La Poste"
    assert payload["rate_limit"]["limit"] == 20


@respx.mock
def test_resource_shares_the_cache_with_the_tool(
    mock_carriers: MockCarriers, delivery: dict[str, Any]
) -> None:
    """Reading the resource must not quietly drain the tool's hourly budget."""
    mock_carriers()
    route = _deliveries_route(delivery)

    list_deliveries("active")
    deliveries_resource("active")

    assert route.call_count == 1


@respx.mock
def test_resource_rejects_an_unknown_filter_mode_without_a_request(
    mock_carriers: MockCarriers,
) -> None:
    mock_carriers()
    route = _deliveries_route()

    with pytest.raises(ParcelError, match="parcel://deliveries/active"):
        deliveries_resource("everything")

    assert route.call_count == 0


@respx.mock
def test_resource_reads_the_per_request_key(mock_carriers: MockCarriers) -> None:
    mock_carriers()
    route = respx.get(DELIVERIES_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "deliveries": []})
    )

    deliveries_resource("active", ctx=_ctx({"x-parcel-token": OTHER_TOKEN}))

    assert route.calls.last.request.headers["api-key"] == OTHER_TOKEN
