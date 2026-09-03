"""Parsing must never turn an unreadable page into a plausible reading.

Every case here is a page BoilerJuice could realistically serve: a redesign,
a session that expired mid-poll, a response cut short by a proxy. The old
parser answered all of them with "0 litres, 0%", which the consumption engine
then recorded as the whole tank being burnt in one hour.
"""

from __future__ import annotations

import pytest

from custom_components.boilerjuice.coordinator import (
    BoilerJuiceAuthError,
    BoilerJuiceParseError,
    looks_like_login_page,
    parse_tank_ids,
    parse_tank_page,
    validate_tank_id,
)

from .helpers import load_fixture


def test_parses_the_current_page_layout() -> None:
    data = parse_tank_page(load_fixture("tank_current.html"), "123456")

    assert data["id"] == "123456"
    assert data["total_level_percentage"] == 62.5
    assert data["usable_level_percentage"] == 62.5
    assert data["current_volume_litres"] == 1562
    assert data["usable_volume_litres"] == 1562
    assert data["capacity_litres"] == 2500
    assert data["height_cm"] == 120
    assert data["name"] == "Garden Tank"
    assert data["manufacturer"] == "Harlequin"
    assert data["model"] == "H2500T"
    assert data["shape"] == "Horizontal Cylinder"
    assert data["oil_type"] == "Kerosene"


def test_parses_the_legacy_page_layout() -> None:
    data = parse_tank_page(load_fixture("tank_legacy.html"), "123456")

    assert data["total_level_percentage"] == 40
    assert data["usable_volume_litres"] == 1000
    assert data["capacity_litres"] == 2500
    assert data["height_cm"] == 120


@pytest.mark.parametrize(
    "fixture",
    ["tank_truncated.html", "tank_redesigned.html"],
)
def test_unreadable_pages_raise_rather_than_reading_zero(fixture: str) -> None:
    with pytest.raises(BoilerJuiceParseError):
        parse_tank_page(load_fixture(fixture), "123456")


def test_empty_body_raises() -> None:
    with pytest.raises(BoilerJuiceParseError):
        parse_tank_page("", "123456")


def test_login_page_raises_auth_rather_than_parse_error() -> None:
    with pytest.raises(BoilerJuiceAuthError):
        parse_tank_page(load_fixture("login.html"), "123456")


def test_partial_page_keeps_the_level_and_omits_the_volume() -> None:
    """A level with no volume is a usable reading; the volume must stay absent.

    Zero-filling the missing volume is what booked a whole tank of phantom
    consumption on the next poll.
    """
    data = parse_tank_page(load_fixture("tank_partial.html"), "123456")

    assert data["total_level_percentage"] == 55
    assert "usable_volume_litres" not in data
    assert "current_volume_litres" not in data
    assert "capacity_litres" not in data


def test_non_numeric_tank_id_is_refused() -> None:
    with pytest.raises(BoilerJuiceParseError):
        parse_tank_page(load_fixture("tank_current.html"), "../../admin")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123456", "123456"),
        (" 123456 ", "123456"),
        (123456, "123456"),
        ("12345a", None),
        ("../123", None),
        ("", None),
        (None, None),
        ("1234567890123", None),
    ],
)
def test_validate_tank_id(raw: object, expected: str | None) -> None:
    assert validate_tank_id(raw) == expected


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("login.html", True),
        ("tank_current.html", False),
        ("tanks_list.html", False),
    ],
)
def test_looks_like_login_page(fixture: str, expected: bool) -> None:
    assert looks_like_login_page(load_fixture(fixture)) is expected


def test_parse_tank_ids_deduplicates_and_preserves_order() -> None:
    assert parse_tank_ids(load_fixture("tanks_list_multiple.html")) == [
        "123456",
        "789012",
    ]


def test_parse_tank_ids_on_an_account_with_no_tanks() -> None:
    assert parse_tank_ids(load_fixture("tanks_list_empty.html")) == []


@pytest.mark.parametrize(
    "percentage",
    ["nan", "inf", "-inf", "-1", "101", "", "not-a-number"],
)
def test_out_of_range_percentages_are_dropped(percentage: str) -> None:
    """An out-of-range level must be absent, not clamped or zeroed."""
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="'
        + percentage
        + '"></div></div><p>900 litres of oil</p>'
    )
    data = parse_tank_page(html, "123456")

    assert "total_level_percentage" not in data
    assert data["usable_volume_litres"] == 900


@pytest.mark.parametrize("capacity", ["0", "-500", "999999999", "nan", "abc"])
def test_out_of_range_capacities_are_dropped(capacity: str) -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tank_size" value="' + capacity + '">'
    )
    data = parse_tank_page(html, "123456")

    assert "capacity_litres" not in data
    assert data["total_level_percentage"] == 50


def test_a_tank_with_no_model_json_still_parses() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="42">'
    )
    data = parse_tank_page(html, "123456")

    assert data["model_id"] == "42"
    assert "model" not in data


def test_malformed_model_json_does_not_fail_the_reading() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="42">'
        "<script>var jsonData = [{oops;</script>"
    )
    data = parse_tank_page(html, "123456")

    assert data["total_level_percentage"] == 50
    assert "model" not in data


def test_an_unclosed_model_json_array_does_not_fail_the_reading() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="42">'
        '<script>var jsonData = [{"id": 42, "tank": {"Brand": "Titan"}}</script>'
    )
    data = parse_tank_page(html, "123456")

    assert "model" not in data


def test_a_model_id_absent_from_the_json_leaves_the_model_unset() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="99">'
        '<script>var jsonData = [{"id": 42, "tank": {"Brand": "Titan",'
        ' "Description": "ES2500"}}];</script>'
    )
    data = parse_tank_page(html, "123456")

    assert data["model_id"] == "99"
    assert "model" not in data


def test_nested_arrays_in_the_model_json_are_walked_correctly() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="42">'
        '<script>var jsonData = [{"id": 41, "sizes": [1, 2, 3]},'
        ' {"id": 42, "tank": {"Brand": "Titan", "Description": "ES2500"}}];</script>'
    )
    data = parse_tank_page(html, "123456")

    assert data["manufacturer"] == "Titan"
    assert data["model"] == "ES2500"


def test_a_volume_mentioned_without_the_oil_keyword_is_not_read() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        "<p>Your last delivery was 900 litres.</p>"
    )
    data = parse_tank_page(html, "123456")

    assert "usable_volume_litres" not in data


def test_an_out_of_range_height_is_dropped() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="internal_height" value="99999">'
    )
    data = parse_tank_page(html, "123456")

    assert "height_cm" not in data
