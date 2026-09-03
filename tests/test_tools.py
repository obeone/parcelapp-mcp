"""Tests for the three MCP tools.

``@mcp.tool()`` registers the function and returns it unchanged, so the tools
are called here as plain functions rather than through ``mcp.call_tool``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from parcel_mcp.client import DELIVERIES_TTL, ParcelError
from parcel_mcp.server import add_delivery, list_deliveries, search_carriers

from .conftest import ADD_DELIVERY_URL, DELIVERIES_URL, FakeClock, MockCarriers


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
