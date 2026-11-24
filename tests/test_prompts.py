"""Tests for the three MCP prompts.

``@mcp.prompt()`` returns the function unchanged, so most checks call the
prompts directly. The registration tests go through the server, since the
argument list and titles a client sees come from the SDK, not from the
function.
"""

from __future__ import annotations

import asyncio

import pytest
import respx
from mcp_types import GetPromptResult, TextContent

from parcel_mcp.server import arriving_soon, check_delivery, mcp, track_parcel


def test_the_three_prompts_are_registered_with_their_arguments() -> None:
    """A client must see each prompt, its title and which arguments are required."""
    prompts = {p.name: p for p in asyncio.run(mcp.list_prompts())}

    assert set(prompts) == {"track_parcel", "arriving_soon", "check_delivery"}
    assert all(p.title and p.description for p in prompts.values())

    def required(name: str) -> dict[str, bool]:
        """Map each argument of prompt ``name`` to its required flag."""
        return {a.name: bool(a.required) for a in prompts[name].arguments or []}

    assert required("track_parcel") == {
        "tracking_number": True,
        "description": False,
        "carrier": False,
    }
    assert required("check_delivery") == {"query": True}
    assert required("arriving_soon") == {}
    # Argument descriptions are what a client shows next to the input field.
    assert all(a.description for a in prompts["track_parcel"].arguments or [])


def test_get_prompt_renders_a_single_user_message() -> None:
    """Going through the SDK yields one user message carrying the brief."""
    result = asyncio.run(mcp.get_prompt("track_parcel", {"tracking_number": "6A12345678901"}))

    assert isinstance(result, GetPromptResult)
    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.role == "user"
    assert isinstance(message.content, TextContent)
    assert "6A12345678901" in message.content.text


def test_get_prompt_rejects_a_missing_required_argument() -> None:
    """The SDK refuses to render track_parcel without a tracking number."""
    with pytest.raises(ValueError, match="tracking_number"):
        asyncio.run(mcp.get_prompt("track_parcel", {}))


def test_track_parcel_uses_the_given_carrier_and_label() -> None:
    """Hints the user supplied are passed on, stripped, instead of asked for again."""
    text = track_parcel(" 6A123 ", description=" New shoes ", carrier=" Colissimo ")

    assert text.startswith('Add the parcel 6A123 to Parcel, labelled "New shoes".')
    assert "The carrier is Colissimo." in text
    assert "Suggest a short label" not in text
    assert "ask me rather than guessing" not in text


def test_track_parcel_without_a_carrier_asks_instead_of_guessing() -> None:
    """With no carrier hint the model is told to ask, not infer from the format."""
    text = track_parcel("6A123")

    assert "ask me rather than guessing" in text
    assert "Suggest a short label too." in text


def test_track_parcel_confirms_before_spending_the_daily_budget() -> None:
    """The carrier is checked first, then the user confirms before the costly add."""
    text = track_parcel("6A123", carrier="Bpost")

    assert text.index("Look the carrier up first") < text.index("wait for my OK")
    assert "postcode or an email" in text
    assert "20 additions a day, and failed ones count" in text


def test_arriving_soon_asks_for_one_sorted_look() -> None:
    """Soonest first, from one fetch: sorting is free, refetching is not."""
    text = arriving_soon()

    assert text.startswith("What parcels am I waiting for?")
    assert "soonest first" in text
    assert "no need to fetch it again" in text


def test_check_delivery_includes_delivered_parcels_and_never_adds() -> None:
    """Delivered parcels stay findable, and a miss must not trigger add_delivery."""
    text = check_delivery(" shoes ")

    assert text.startswith('Where is my parcel "shoes"?')
    assert "may already be delivered" in text
    assert "Don't add anything to Parcel." in text


def test_prompts_read_as_plain_user_requests() -> None:
    """No tool or parameter names leak into text the client shows as the user's."""
    texts = [track_parcel("6A123"), arriving_soon(), check_delivery("shoes")]
    jargon = ("list_deliveries", "add_delivery", "search_carriers", "filter_mode", "sort_by")

    for text in texts:
        assert not any(word in text for word in jargon), text


@respx.mock
def test_rendering_the_prompts_makes_no_request() -> None:
    """Prompts are free: with no route mocked, any request would fail the test."""
    track_parcel("6A123")
    arriving_soon()
    check_delivery("shoes")
    asyncio.run(mcp.get_prompt("check_delivery", {"query": "shoes"}))

    assert not respx.calls
