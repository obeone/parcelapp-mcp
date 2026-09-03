"""Tests for the HTTP layer: auth, the response envelope, and the TTL cache."""

from __future__ import annotations

import httpx
import pytest
import respx

from parcel_mcp import client
from parcel_mcp.client import ParcelError

from .conftest import DELIVERIES_URL, FAKE_TOKEN


def test_api_key_reads_parcel_token() -> None:
    assert client.api_key() == FAKE_TOKEN


def test_api_key_falls_back_to_parcel_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PARCEL_TOKEN")
    monkeypatch.setenv("PARCEL_API_KEY", "fallback-token")
    assert client.api_key() == "fallback-token"


def test_missing_api_key_names_the_variable_to_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PARCEL_TOKEN")
    with pytest.raises(ParcelError, match="PARCEL_TOKEN"):
        client.api_key()


@respx.mock
def test_request_sends_the_key_as_a_header() -> None:
    route = respx.get(DELIVERIES_URL).mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.request("GET", DELIVERIES_URL)
    assert route.calls.last.request.headers["api-key"] == FAKE_TOKEN


@respx.mock
def test_request_returns_the_decoded_envelope() -> None:
    respx.get(DELIVERIES_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "deliveries": []})
    )
    assert client.request("GET", DELIVERIES_URL) == {"success": True, "deliveries": []}


@respx.mock
def test_success_false_surfaces_the_upstream_message() -> None:
    respx.get(DELIVERIES_URL).mock(
        return_value=httpx.Response(
            200, json={"success": False, "error_message": "Tracking number not recognised"}
        )
    )
    with pytest.raises(ParcelError, match="Tracking number not recognised"):
        client.request("GET", DELIVERIES_URL)


@respx.mock
def test_success_false_without_a_message_still_fails() -> None:
    """The API is undocumented; error_message may well be absent or empty."""
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(200, json={"success": False}))
    with pytest.raises(ParcelError, match="failed"):
        client.request("GET", DELIVERIES_URL)


@respx.mock
def test_401_points_at_the_api_key() -> None:
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(ParcelError, match="API key"):
        client.request("GET", DELIVERIES_URL)


@respx.mock
def test_429_states_both_rate_limits() -> None:
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(429, text="slow down"))
    with pytest.raises(ParcelError, match="20 delivery listings per hour"):
        client.request("GET", DELIVERIES_URL)


@respx.mock
def test_non_json_body_is_reported_with_its_status() -> None:
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(502, text="<html>gateway</html>"))
    with pytest.raises(ParcelError, match="non-JSON content \\(HTTP 502\\)"):
        client.request("GET", DELIVERIES_URL)


@respx.mock
def test_json_that_is_not_an_object_is_rejected() -> None:
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(200, json=["unexpected"]))
    with pytest.raises(ParcelError, match="unexpected JSON shape"):
        client.request("GET", DELIVERIES_URL)


@respx.mock
def test_transport_failure_becomes_a_parcel_error() -> None:
    respx.get(DELIVERIES_URL).mock(side_effect=httpx.ConnectError("no route to host"))
    with pytest.raises(ParcelError, match="Request to Parcel failed"):
        client.request("GET", DELIVERIES_URL)


def test_cache_returns_the_value_until_the_ttl_lapses(clock) -> None:
    client.store("k", "value")
    assert client.cached("k", ttl=10.0) == "value"

    clock.advance(9.0)
    assert client.cached("k", ttl=10.0) == "value"

    clock.advance(2.0)  # now 11 s old, past the TTL
    assert client.cached("k", ttl=10.0) is None


def test_cache_miss_returns_none() -> None:
    assert client.cached("never-written", ttl=10.0) is None


def test_clear_cache_drops_named_keys_only() -> None:
    client.store("a", 1)
    client.store("b", 2)
    client.clear_cache("a")
    assert client.cached("a", ttl=10.0) is None
    assert client.cached("b", ttl=10.0) == 2


def test_clear_cache_without_arguments_empties_everything() -> None:
    client.store("a", 1)
    client.store("b", 2)
    client.clear_cache()
    assert client.cached("a", ttl=10.0) is None
    assert client.cached("b", ttl=10.0) is None


@respx.mock
def test_carrier_catalogue_is_fetched_once(mock_carriers) -> None:
    route = mock_carriers()
    client.load_carriers()
    client.load_carriers()
    assert route.call_count == 1


@respx.mock
def test_carrier_catalogue_failure_is_reported(mock_carriers) -> None:
    mock_carriers(status_code=500)
    with pytest.raises(ParcelError, match="Could not fetch the carrier list"):
        client.load_carriers()


@respx.mock
def test_carrier_name_resolves_a_known_code(mock_carriers) -> None:
    mock_carriers()
    assert client.carrier_name("lp") == "La Poste"


@respx.mock
def test_carrier_name_falls_back_to_the_code(mock_carriers) -> None:
    """An unreachable catalogue must degrade a listing, not break it."""
    mock_carriers(status_code=500)
    assert client.carrier_name("lp") == "lp"


@respx.mock
def test_carrier_name_falls_back_for_an_unknown_code(mock_carriers) -> None:
    mock_carriers()
    assert client.carrier_name("does-not-exist") == "does-not-exist"
