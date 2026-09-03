"""Tests for the HTTP layer: auth, the response envelope, and the TTL cache."""

from __future__ import annotations

import httpx
import pytest
import respx

from parcel_mcp import client
from parcel_mcp.client import ParcelError

from .conftest import DELIVERIES_URL, FAKE_TOKEN, OTHER_TOKEN, FakeClock, MockCarriers


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
    route = respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(200, json={"success": True}))
    client.request("GET", DELIVERIES_URL, FAKE_TOKEN)
    assert route.calls.last.request.headers["api-key"] == FAKE_TOKEN


@respx.mock
def test_request_returns_the_decoded_envelope() -> None:
    respx.get(DELIVERIES_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "deliveries": []})
    )
    assert client.request("GET", DELIVERIES_URL, FAKE_TOKEN) == {"success": True, "deliveries": []}


@respx.mock
def test_success_false_surfaces_the_upstream_message() -> None:
    respx.get(DELIVERIES_URL).mock(
        return_value=httpx.Response(
            200, json={"success": False, "error_message": "Tracking number not recognised"}
        )
    )
    with pytest.raises(ParcelError, match="Tracking number not recognised"):
        client.request("GET", DELIVERIES_URL, FAKE_TOKEN)


@respx.mock
def test_success_false_without_a_message_still_fails() -> None:
    """The API is undocumented; error_message may well be absent or empty."""
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(200, json={"success": False}))
    with pytest.raises(ParcelError, match="failed"):
        client.request("GET", DELIVERIES_URL, FAKE_TOKEN)


@respx.mock
def test_401_points_at_the_api_key() -> None:
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(ParcelError, match="API key"):
        client.request("GET", DELIVERIES_URL, FAKE_TOKEN)


@respx.mock
def test_429_states_both_rate_limits() -> None:
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(429, text="slow down"))
    with pytest.raises(ParcelError, match="20 delivery listings per hour"):
        client.request("GET", DELIVERIES_URL, FAKE_TOKEN)


@respx.mock
def test_non_json_body_is_reported_with_its_status() -> None:
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(502, text="<html>gateway</html>"))
    with pytest.raises(ParcelError, match="non-JSON content \\(HTTP 502\\)"):
        client.request("GET", DELIVERIES_URL, FAKE_TOKEN)


@respx.mock
def test_json_that_is_not_an_object_is_rejected() -> None:
    respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(200, json=["unexpected"]))
    with pytest.raises(ParcelError, match="unexpected JSON shape"):
        client.request("GET", DELIVERIES_URL, FAKE_TOKEN)


@respx.mock
def test_transport_failure_becomes_a_parcel_error() -> None:
    respx.get(DELIVERIES_URL).mock(side_effect=httpx.ConnectError("no route to host"))
    with pytest.raises(ParcelError, match="Request to Parcel failed"):
        client.request("GET", DELIVERIES_URL, FAKE_TOKEN)


def test_cache_returns_the_value_until_the_ttl_lapses(clock: FakeClock) -> None:
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
def test_carrier_catalogue_is_fetched_once(mock_carriers: MockCarriers) -> None:
    route = mock_carriers()
    client.load_carriers()
    client.load_carriers()
    assert route.call_count == 1


@respx.mock
def test_carrier_catalogue_failure_is_reported(mock_carriers: MockCarriers) -> None:
    mock_carriers(status_code=500)
    with pytest.raises(ParcelError, match="Could not fetch the carrier list"):
        client.load_carriers()


@respx.mock
def test_carrier_name_resolves_a_known_code(mock_carriers: MockCarriers) -> None:
    mock_carriers()
    assert client.carrier_name("lp") == "La Poste"


@respx.mock
def test_carrier_name_falls_back_to_the_code(mock_carriers: MockCarriers) -> None:
    """An unreachable catalogue must degrade a listing, not break it."""
    mock_carriers(status_code=500)
    assert client.carrier_name("lp") == "lp"


@respx.mock
def test_carrier_name_falls_back_for_an_unknown_code(mock_carriers: MockCarriers) -> None:
    mock_carriers()
    assert client.carrier_name("does-not-exist") == "does-not-exist"


# --------------------------------------------------------------------------- #
# Key fingerprints and the rate-limit counters
# --------------------------------------------------------------------------- #


def test_fingerprint_is_stable_and_hides_the_key() -> None:
    fingerprint = client.key_fingerprint(FAKE_TOKEN)
    assert fingerprint == client.key_fingerprint(FAKE_TOKEN)
    assert len(fingerprint) == 16
    assert FAKE_TOKEN not in fingerprint


def test_fingerprint_separates_two_keys() -> None:
    assert client.key_fingerprint(FAKE_TOKEN) != client.key_fingerprint(OTHER_TOKEN)


def test_budget_starts_untouched() -> None:
    report = client.budget(client.DELIVERIES_BUCKET, "fp")

    assert report["limit"] == 20
    assert report["per"] == "hour"
    assert report["spent_by_this_server"] == 0
    assert report["remaining_at_most"] == 20


def test_budget_counts_recorded_requests() -> None:
    for _ in range(3):
        client.record_request(client.DELIVERIES_BUCKET, "fp")

    report = client.budget(client.DELIVERIES_BUCKET, "fp")

    assert report["spent_by_this_server"] == 3
    assert report["remaining_at_most"] == 17


def test_budget_forgets_requests_older_than_the_window(clock: FakeClock) -> None:
    client.record_request(client.DELIVERIES_BUCKET, "fp")
    clock.advance(1800)
    client.record_request(client.DELIVERIES_BUCKET, "fp")
    assert client.budget(client.DELIVERIES_BUCKET, "fp")["spent_by_this_server"] == 2

    # Push the first request out of the rolling hour, but not the second.
    clock.advance(1801)

    assert client.budget(client.DELIVERIES_BUCKET, "fp")["spent_by_this_server"] == 1


def test_budget_never_reports_a_negative_remainder() -> None:
    for _ in range(25):
        client.record_request(client.ADD_BUCKET, "fp")

    report = client.budget(client.ADD_BUCKET, "fp")

    assert report["spent_by_this_server"] == 25
    assert report["remaining_at_most"] == 0


def test_budgets_are_separate_per_key() -> None:
    client.record_request(client.DELIVERIES_BUCKET, client.key_fingerprint(FAKE_TOKEN))

    mine = client.budget(client.DELIVERIES_BUCKET, client.key_fingerprint(FAKE_TOKEN))
    theirs = client.budget(client.DELIVERIES_BUCKET, client.key_fingerprint(OTHER_TOKEN))

    assert mine["spent_by_this_server"] == 1
    assert theirs["spent_by_this_server"] == 0


def test_budgets_are_separate_per_bucket() -> None:
    client.record_request(client.DELIVERIES_BUCKET, "fp")

    assert client.budget(client.ADD_BUCKET, "fp")["spent_by_this_server"] == 0
    assert client.budget(client.ADD_BUCKET, "fp")["per"] == "day"


def test_cache_age_reports_how_stale_a_value_is(clock: FakeClock) -> None:
    assert client.cache_age("k") is None

    client.store("k", "value")
    assert client.cache_age("k") == 0

    clock.advance(12.5)
    assert client.cache_age("k") == 12.5


@respx.mock
def test_request_sends_the_key_it_was_given_not_the_environment() -> None:
    route = respx.get(DELIVERIES_URL).mock(return_value=httpx.Response(200, json={"success": True}))

    client.request("GET", DELIVERIES_URL, OTHER_TOKEN)

    assert route.calls.last.request.headers["api-key"] == OTHER_TOKEN
