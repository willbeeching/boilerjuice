"""Parsing must never turn an unreadable page into a plausible reading.

Every case here is a page BoilerJuice could realistically serve: a redesign,
a session that expired mid-poll, a response cut short by a proxy. The old
parser answered all of them with "0 litres, 0%", which the consumption engine
then recorded as the whole tank being burnt in one hour.
"""

from __future__ import annotations

import json

import pytest
from custom_components.boilerjuice import parser
from custom_components.boilerjuice.errors import (
    BoilerJuiceAuthError,
    BoilerJuiceParseError,
)
from custom_components.boilerjuice.parser import (
    looks_like_login_page,
    parse_tank_ids,
    parse_tank_page,
    validate_tank_id,
)

from .helpers import load_fixture


def test_parses_the_current_page_layout() -> None:
    reading = parse_tank_page(load_fixture("tank_current.html"), "123456")

    assert reading.tank_id == "123456"
    assert reading.level_percentage == 62.5
    assert reading.level_percentage == 62.5
    assert reading.volume_litres == 1562
    assert reading.volume_litres == 1562
    assert reading.capacity_litres == 2500
    assert reading.height_cm == 120
    assert reading.name == "Garden Tank"
    assert reading.manufacturer == "Harlequin"
    assert reading.model == "H2500T"
    assert reading.shape == "Horizontal Cylinder"
    assert reading.oil_type == "Kerosene"


def test_parses_the_legacy_page_layout() -> None:
    reading = parse_tank_page(load_fixture("tank_legacy.html"), "123456")

    assert reading.level_percentage == 40
    assert reading.volume_litres == 1000
    assert reading.capacity_litres == 2500
    assert reading.height_cm == 120


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


def test_parses_the_renamed_name_and_oil_type_fields() -> None:
    """The edit page now uses id=name and id=oil_type_id."""
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="name" name="name" value="Garden Tank">'
        '<select id="oil_type_id"><option>Gas Oil</option>'
        "<option selected>Kerosene</option></select>"
    )
    reading = parse_tank_page(html, "123456")

    assert reading.name == "Garden Tank"
    assert reading.oil_type == "Kerosene"


def test_partial_page_keeps_the_level_and_omits_the_volume() -> None:
    """A level with no volume is a usable reading; the volume must stay absent.

    Zero-filling the missing volume is what booked a whole tank of phantom
    consumption on the next poll.
    """
    reading = parse_tank_page(load_fixture("tank_partial.html"), "123456")

    assert reading.level_percentage == 55
    assert reading.volume_litres is None
    assert reading.volume_litres is None
    assert reading.capacity_litres is None


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
    reading = parse_tank_page(html, "123456")

    assert reading.level_percentage is None
    assert reading.volume_litres == 900


@pytest.mark.parametrize("capacity", ["0", "-500", "999999999", "nan", "abc"])
def test_out_of_range_capacities_are_dropped(capacity: str) -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tank_size" value="' + capacity + '">'
    )
    reading = parse_tank_page(html, "123456")

    assert reading.capacity_litres is None
    assert reading.level_percentage == 50


def test_a_tank_with_no_model_json_still_parses() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="42">'
    )
    reading = parse_tank_page(html, "123456")

    assert reading.model_id == "42"
    assert reading.model is None


def test_malformed_model_json_does_not_fail_the_reading() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="42">'
        "<script>var jsonData = [{oops;</script>"
    )
    reading = parse_tank_page(html, "123456")

    assert reading.level_percentage == 50
    assert reading.model is None


def test_an_unclosed_model_json_array_does_not_fail_the_reading() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="42">'
        '<script>var jsonData = [{"id": 42, "tank": {"Brand": "Titan"}}</script>'
    )
    reading = parse_tank_page(html, "123456")

    assert reading.model is None


def test_a_model_id_absent_from_the_json_leaves_the_model_unset() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="99">'
        '<script>var jsonData = [{"id": 42, "tank": {"Brand": "Titan",'
        ' "Description": "ES2500"}}];</script>'
    )
    reading = parse_tank_page(html, "123456")

    assert reading.model_id == "99"
    assert reading.model is None


def test_nested_arrays_in_the_model_json_are_walked_correctly() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="42">'
        '<script>var jsonData = [{"id": 41, "sizes": [1, 2, 3]},'
        ' {"id": 42, "tank": {"Brand": "Titan", "Description": "ES2500"}}];</script>'
    )
    reading = parse_tank_page(html, "123456")

    assert reading.manufacturer == "Titan"
    assert reading.model == "ES2500"


def test_a_volume_mentioned_without_the_oil_keyword_is_not_read() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        "<p>Your last delivery was 900 litres.</p>"
    )
    reading = parse_tank_page(html, "123456")

    assert reading.volume_litres is None


def test_an_out_of_range_height_is_dropped() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="internal_height" value="99999">'
    )
    reading = parse_tank_page(html, "123456")

    assert reading.height_cm is None


def test_an_empty_account_is_recognised_by_its_empty_state() -> None:
    assert parse_tank_ids(load_fixture("tanks_list_empty.html")) == []


@pytest.mark.parametrize(
    "html",
    [
        pytest.param("<p>You have no tanks yet.</p>", id="no-tanks"),
        pytest.param("<p>You have not added a tank yet.</p>", id="not-added-a-tank"),
        pytest.param("<p>You haven't added any tanks.</p>", id="havent-added-any"),
        pytest.param("<p>You don't have any tanks.</p>", id="dont-have-any"),
        pytest.param(
            '<a href="/uk/users/tanks/new">Add your first tank</a>', id="first-tank"
        ),
    ],
)
def test_an_explicitly_empty_account_returns_no_tanks(html: str) -> None:
    """Only a statement that there are no tanks counts as one."""
    assert parse_tank_ids(html) == []


@pytest.mark.parametrize(
    "html",
    [
        pytest.param(
            "<h1>Your tanks</h1>"
            '<div class="tank-card" data-tank="123456"><h2>Garden Tank</h2>'
            "<span>62%</span></div>"
            '<a href="/uk/users/tanks/new">Add another tank</a>',
            id="populated-but-redesigned",
        ),
        pytest.param(
            '<a href="/uk/users/tanks/new">Add a tank</a>', id="add-link-alone"
        ),
        pytest.param(
            '<button data-action="tanks#add">Add tank</button>', id="add-button"
        ),
    ],
)
def test_an_invitation_to_add_a_tank_is_not_proof_of_an_empty_account(
    html: str,
) -> None:
    """An add-tank control sits happily on a populated page.

    Accepting the add-tank control meant a populated account whose tank
    markup had changed parsed as empty, and its devices were retired after
    three polls without the layout repair ever being raised.
    """
    with pytest.raises(BoilerJuiceParseError):
        parse_tank_ids(html)


@pytest.mark.parametrize(
    "html",
    [
        pytest.param("", id="empty"),
        pytest.param(
            "<html><body><h1>Your account</h1></body></html>", id="redesigned"
        ),
        pytest.param(
            "<html><body><div id='tanks-app'></div></body></html>", id="js-only"
        ),
        pytest.param(
            "<html><body><p>Something went wrong.</p></body></html>", id="error-page"
        ),
    ],
)
def test_an_unrecognised_listing_is_not_proof_that_the_tanks_are_gone(
    html: str,
) -> None:
    """The same mistake the tank page used to make, one page earlier.

    An empty list is acted on: the coordinator removes devices after three
    of them. "We no longer understand this page" must not be able to say
    that.
    """
    with pytest.raises(BoilerJuiceParseError):
        parse_tank_ids(html)


@pytest.mark.parametrize(
    "status_line",
    [
        pytest.param("No tanks need a delivery today", id="delivery"),
        pytest.param("No tanks need filling this week", id="filling"),
        pytest.param("No tanks require attention", id="attention"),
        pytest.param("Good news: no tanks need topping up", id="topping-up"),
        pytest.param("No tanks are low", id="low"),
    ],
)
def test_a_status_line_mentioning_tanks_is_not_an_empty_account(
    status_line: str,
) -> None:
    """A bare "no tanks" described the very tanks it would have retired.

    A footer reading "No tanks need a delivery today" sits on a populated
    page. Searching the whole page for the phrase accepted it as proof the
    account was empty; the message has to match in full.
    """
    html = (
        "<html><body><h1>Your tanks</h1>"
        '<div class="card" data-tank="123456">Garden Tank</div>'
        f"<footer>{status_line}</footer></body></html>"
    )

    with pytest.raises(BoilerJuiceParseError):
        parse_tank_ids(html)


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("You have no tanks yet.", id="have-no-tanks"),
        pytest.param("There are no tanks.", id="there-are-none"),
        pytest.param("No tanks added", id="none-added"),
        pytest.param("You have not added a tank yet.", id="not-added"),
        pytest.param("You haven’t added any tanks", id="curly-apostrophe"),  # noqa: RUF001
        pytest.param("You don’t have any tanks", id="dont-have-any"),  # noqa: RUF001
        pytest.param("— No tanks —", id="decorated"),
    ],
)
def test_a_complete_empty_account_message_is_accepted(message: str) -> None:
    assert parse_tank_ids(f"<html><body><p>{message}</p></body></html>") == []


@pytest.mark.parametrize(
    "status_line",
    [
        pytest.param(
            "<strong>No tanks</strong> need a delivery today", id="strong-prefix"
        ),
        pytest.param(
            "<span>No tanks</span> require <em>attention</em>", id="span-and-em"
        ),
        pytest.param(
            "Good news: <b>no tanks</b> need topping up", id="bold-mid-sentence"
        ),
        pytest.param(
            'No <a href="/uk/help/levels">tanks</a> are low', id="linked-word"
        ),
    ],
)
def test_inline_markup_does_not_split_a_status_line_into_an_empty_state(
    status_line: str,
) -> None:
    """Half a sentence is not a statement about the account.

    Matching text nodes one at a time offered "No tanks" on its own the
    moment two words of the sentence were wrapped in <strong>, which is the
    same false empty account by a different route. The block element holding
    the sentence is what has to match.
    """
    html = (
        "<html><body><h1>Your tanks</h1>"
        '<div class="card" data-tank="123456">Garden Tank</div>'
        f"<footer><p>{status_line}</p></footer></body></html>"
    )

    with pytest.raises(BoilerJuiceParseError):
        parse_tank_ids(html)


def test_an_empty_state_carrying_inline_markup_is_still_recognised() -> None:
    """Emphasis inside the message must not stop it being read."""
    html = (
        "<html><body><h1>Your tanks</h1>"
        "<div class='empty'><p>You have <strong>no tanks</strong> yet.</p>"
        '<a href="/uk/users/tanks/new">Add your first tank</a></div>'
        "</body></html>"
    )

    assert parse_tank_ids(html) == []


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param('{"id": 42}', id="object-not-a-list"),
        pytest.param('"Titan"', id="a-bare-string"),
        pytest.param("42", id="a-bare-number"),
        pytest.param('["just a string"]', id="entry-is-a-string"),
        pytest.param("[[1, 2, 3]]", id="entry-is-a-list"),
        pytest.param('[{"id": 42, "tank": null}]', id="tank-is-null"),
        pytest.param('[{"id": 42, "tank": "Titan"}]', id="tank-is-a-string"),
        pytest.param('[{"id": 42, "tank": {"Brand": {"en": "Titan"}}}]', id="nested"),
        pytest.param('[{"id": 42, "tank": {"Brand": 7}}]', id="a-number-for-a-name"),
        pytest.param('[{"id": 42, "tank": {"Brand": "   "}}]', id="blank"),
    ],
)
def test_malformed_model_json_costs_the_model_and_nothing_else(payload: str) -> None:
    """The model blob is optional decoration; the level is the reading.

    Walking it on trust raised AttributeError out of the parser, so a tank
    whose model JSON changed shape lost its level, its volume and everything
    else with it.
    """
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        "<p>900 litres of oil</p>"
        '<input id="tankModelInput" value="42">'
        f"<script>var jsonData = {payload};</script>"
    )

    reading = parse_tank_page(html, "123456")

    assert reading.level_percentage == 50
    assert reading.volume_litres == 900
    assert reading.model_id == "42"
    assert reading.model is None
    assert reading.manufacturer is None


def test_a_well_formed_model_entry_is_still_read() -> None:
    """The check must not throw the good case out with the bad."""
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="42">'
        '<script>var jsonData = [{"id": 41, "tank": {"Brand": "Other"}},'
        ' {"id": 42, "tank": {"Brand": "Titan", "Description": "ES2500"}}];</script>'
    )

    reading = parse_tank_page(html, "123456")

    assert reading.manufacturer == "Titan"
    assert reading.model == "ES2500"


def test_a_malformed_entry_does_not_hide_a_good_one_after_it() -> None:
    """One bad entry in the list is skipped, not treated as the end of it."""
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<input id="tankModelInput" value="42">'
        '<script>var jsonData = ["rubbish", 7, null,'
        ' {"id": 42, "tank": {"Brand": "Titan", "Description": "ES2500"}}];</script>'
    )

    reading = parse_tank_page(html, "123456")

    assert reading.manufacturer == "Titan"
    assert reading.model == "ES2500"


# --- describing a page we cannot read -------------------------------------

CHALLENGE = """
<html><head><title>Just a moment...</title></head>
<body><div class="cf-browser-verification">Checking your browser</div>
<script>window._cf_chl_opt={};</script></body></html>
"""

SECRET_PAGE = """
<html><body>
  <h1>Welcome back, Wilhelmina Bracegirdle</h1>
  <p>will@together.agency</p>
  <p>Tank 998877 at 12 Sycamore Lane, Tunbridge Wells</p>
  <input type="hidden" name="authenticity_token" value="s3cr3t-csrf-value">
</body></html>
"""


def test_the_shape_of_a_challenge_page_is_recognisable() -> None:
    """A layout change and a bot challenge used to look identical."""
    shape = parser.describe_page_shape(CHALLENGE)

    assert shape["looks_like_interstitial"] == ["cloudflare"]
    assert shape["tank_links"] == 0
    assert shape["is_html"] is True


def test_the_shape_of_a_real_tanks_page_names_no_interstitial() -> None:
    shape = parser.describe_page_shape(load_fixture("tanks_list.html"))

    assert shape["looks_like_interstitial"] == []
    assert shape["tank_links"] > 0


def test_the_shape_carries_no_page_content() -> None:
    """Page HTML is never reproduced, at any level. Counts only.

    The whole point of the describer is to be safe to log and to paste
    into an issue, so nothing from the document may survive into it.
    """
    shape = parser.describe_page_shape(SECRET_PAGE)
    serialised = json.dumps(shape)

    for secret in (
        "Wilhelmina",
        "Bracegirdle",
        "will@together.agency",
        "998877",
        "Sycamore",
        "Tunbridge",
        "s3cr3t-csrf-value",
        "authenticity_token",
    ):
        assert secret not in serialised, f"the page shape leaked {secret!r}"

    # Only counts, booleans and our own fixed words.
    assert set(shape) == {
        "bytes",
        "is_html",
        "forms",
        "password_inputs",
        "links",
        "tank_links",
        "scripts",
        "looks_like_interstitial",
    }
    for key, value in shape.items():
        if key == "looks_like_interstitial":
            assert set(value) <= set(parser._INTERSTITIAL_MARKERS)
        else:
            assert isinstance(value, (int, bool))


def test_an_unreadable_tanks_page_reports_its_shape() -> None:
    """The reason a user reads in the UI should say what arrived."""
    with pytest.raises(BoilerJuiceParseError) as caught:
        parse_tank_ids(CHALLENGE)

    assert "cloudflare" in str(caught.value)


# --- the JavaScript app's JSON API ----------------------------------------

JS_SHELL = """
<!DOCTYPE html>
<html><head><title>Your tanks</title>
<script src="/assets/application.js"></script>
<script src="/assets/tanks.js"></script>
</head><body><div id="app"></div></body></html>
"""


def test_a_javascript_shell_is_recognised() -> None:
    assert parser.looks_like_javascript_shell(JS_SHELL)
    assert parser.looks_like_javascript_shell(load_fixture("tanks_list.html")) is False
    assert parser.looks_like_javascript_shell(load_fixture("login.html")) is False


def test_a_javascript_shell_is_not_an_empty_account() -> None:
    """The tanks page is now a JS app: 15 scripts, no links, no forms.

    Treating that as "no tanks" would retire every device on the account.
    """
    with pytest.raises(BoilerJuiceParseError) as caught:
        parse_tank_ids(JS_SHELL)

    assert "page shape" in str(caught.value)
    assert "tank_links" in str(caught.value)


def test_json_listing_returns_ids_in_order() -> None:
    body = json.dumps(
        [
            {"id": 123456, "name": "Garden"},
            {"id": 789012, "name": "Barn"},
            {"id": 123456, "name": "Garden again"},
        ]
    )

    assert parse_tank_ids(body) == ["123456", "789012"]


def test_json_listing_accepts_a_wrapped_empty_list() -> None:
    assert parse_tank_ids('{"tanks": []}') == []


def test_json_listing_an_object_without_tanks_is_a_parse_error() -> None:
    with pytest.raises(BoilerJuiceParseError) as caught:
        parse_tank_ids('{"status": "ok", "count": 2}')

    message = str(caught.value)
    assert "page shape" in message
    assert "key_count" in message
    assert "ok" not in message
    assert "status" not in message


def test_json_listing_of_objects_without_ids_is_not_an_empty_account() -> None:
    """A list we cannot identify is unreadable, not proof the tanks are gone."""
    with pytest.raises(BoilerJuiceParseError) as caught:
        parse_tank_ids('[{"name": "Garden"}]')

    assert "readable tank id" in str(caught.value)
    assert "Garden" not in str(caught.value)
    with pytest.raises(BoilerJuiceParseError):
        parse_tank_ids('{"tanks": [{"name": "Garden"}]}')
    assert parse_tank_ids("[]") == []


def test_json_sign_in_error_is_an_auth_error() -> None:
    with pytest.raises(BoilerJuiceAuthError):
        parse_tank_ids('{"error":"You need to sign in or sign up before continuing."}')


def test_json_tank_parses_a_flat_object() -> None:
    body = json.dumps(
        {
            "id": 123456,
            "name": "Garden Tank",
            "percentage": 62.5,
            "litres": 1562,
            "tank_size": 2500,
            "internal_height": 120,
            "shape": "horizontal_cylinder",
            "oil_type": "Kerosene",
            "model": "H2500T",
            "manufacturer": "Harlequin",
        }
    )
    reading = parse_tank_page(body, "123456")

    assert reading.tank_id == "123456"
    assert reading.level_percentage == 62.5
    assert reading.volume_litres == 1562
    assert reading.capacity_litres == 2500
    assert reading.height_cm == 120
    assert reading.name == "Garden Tank"
    assert reading.shape == "Horizontal Cylinder"
    assert reading.oil_type == "Kerosene"
    assert reading.model == "H2500T"
    assert reading.manufacturer == "Harlequin"


def test_json_tank_reads_a_nested_latest_reading() -> None:
    body = json.dumps(
        {
            "id": 123456,
            "name": "Garden Tank",
            "latest_reading": {"percentage": 40, "litres": 1000},
        }
    )
    reading = parse_tank_page(body, "123456")

    assert reading.level_percentage == 40
    assert reading.volume_litres == 1000
    assert reading.name == "Garden Tank"


def test_json_tank_picks_the_requested_id_from_a_list() -> None:
    body = json.dumps(
        [
            {"id": 111, "percentage": 10, "litres": 100},
            {"id": 123456, "percentage": 80, "litres": 2000},
        ]
    )
    reading = parse_tank_page(body, "123456")

    assert reading.level_percentage == 80
    assert reading.volume_litres == 2000


def test_json_tank_singleton_with_a_different_id_raises() -> None:
    with pytest.raises(BoilerJuiceParseError) as caught:
        parse_tank_page('{"id": 654321, "percentage": 50}', "123456")

    assert "not the one requested" in str(caught.value)
    assert "654321" not in str(caught.value)


def test_json_tank_singleton_without_an_id_is_the_requested_tank() -> None:
    reading = parse_tank_page('[{"percentage": 50, "litres": 1000}]', "123456")

    assert reading.tank_id == "123456"
    assert reading.level_percentage == 50


def test_json_tank_without_a_measurement_raises() -> None:
    with pytest.raises(BoilerJuiceParseError) as caught:
        parse_tank_page('{"id": 123456, "name": "Garden Tank"}', "123456")

    assert "neither an oil level nor an oil volume" in str(caught.value)
    assert "Garden Tank" not in str(caught.value)


def test_json_shape_carries_no_values() -> None:
    data = {
        "name": "Wilhelmina Bracegirdle",
        "email": "will@together.agency",
        "tanks": [{"id": 998877, "address": "12 Sycamore Lane"}],
    }
    serialised = json.dumps(parser.describe_json_shape(data))

    for secret in (
        "Wilhelmina",
        "Bracegirdle",
        "will@together.agency",
        "998877",
        "Sycamore",
        "email",
    ):
        assert secret not in serialised, f"the JSON shape leaked {secret!r}"

    assert parser.describe_json_shape(data) == {
        "is_json": True,
        "type": "object",
        "key_count": 3,
        "recognised_keys": ["name", "tanks"],
        "nested": {"name": "string", "tanks": "list"},
    }


def test_json_shape_does_not_echo_identifier_keys() -> None:
    """Account-specific values used as keys must not reach a diagnostic."""
    data = {998877: {"email": "will@together.agency"}, "name": "Garden"}
    serialised = json.dumps(parser.describe_json_shape(data))

    assert "998877" not in serialised
    assert "will@together.agency" not in serialised
    assert "Garden" not in serialised
    assert parser.describe_json_shape(data)["recognised_keys"] == ["name"]


def test_json_shape_caps_the_recognised_keys_it_names() -> None:
    payload = dict.fromkeys(parser._RECOGNISED_JSON_KEYS, 1)
    payload["not_a_field"] = True
    shape = parser.describe_json_shape(payload)

    assert shape["key_count"] == len(parser._RECOGNISED_JSON_KEYS) + 1
    assert len(shape["recognised_keys"]) == parser._MAX_SHAPE_KEYS
    assert set(shape["recognised_keys"]) <= parser._RECOGNISED_JSON_KEYS
    assert "not_a_field" not in json.dumps(shape)


def test_broken_json_raises_without_quoting_the_body() -> None:
    with pytest.raises(BoilerJuiceParseError) as caught:
        parse_tank_ids('{"name": "Wilhelmina",')

    assert "Wilhelmina" not in str(caught.value)


def test_looks_like_json_sign_in() -> None:
    assert parser.looks_like_json_sign_in(
        '{"error":"You need to sign in or sign up before continuing."}'
    )
    assert parser.looks_like_json_sign_in(
        '\ufeff{"error":"You need to sign in or sign up before continuing."}'
    )
    assert parser.looks_like_json_sign_in("<html></html>") is False
    assert parser.looks_like_json_sign_in("{not-json") is False
    assert parser.looks_like_json_sign_in("[1, 2]") is False


def test_describe_json_shape_of_a_list_and_a_scalar() -> None:
    assert parser.describe_json_shape([{"id": 1}, "skip", {"name": "x"}]) == {
        "is_json": True,
        "type": "list",
        "length": 3,
        "item_key_count": 2,
        "recognised_item_keys": ["id", "name"],
    }
    assert parser.describe_json_shape(7) == {"is_json": True, "type": "number"}
    assert parser.describe_json_shape(None) == {"is_json": True, "type": "null"}
    assert parser.describe_json_shape(True) == {"is_json": True, "type": "boolean"}


def test_json_tank_sign_in_error_is_an_auth_error() -> None:
    with pytest.raises(BoilerJuiceAuthError):
        parse_tank_page(
            '{"error":"You need to sign in or sign up before continuing."}',
            "123456",
        )


def test_json_tank_refuses_a_non_numeric_id() -> None:
    with pytest.raises(BoilerJuiceParseError):
        parse_tank_page('{"id": 1, "percentage": 50}', "../admin")


def test_json_tank_unrecognised_payload_raises() -> None:
    with pytest.raises(BoilerJuiceParseError) as caught:
        parse_tank_page("[1, 2, 3]", "123456")

    assert "page shape" in str(caught.value)


def test_json_tank_list_without_the_requested_id_raises() -> None:
    with pytest.raises(BoilerJuiceParseError):
        parse_tank_page(
            '[{"id": 111, "percentage": 10}, {"id": 222, "percentage": 20}]',
            "123456",
        )


def test_json_tank_reads_oil_type_object_and_numeric_model_id() -> None:
    reading = parse_tank_page(
        json.dumps(
            {
                "id": 123456,
                "percentage": 50,
                "oil_type": {"name": "Kerosene"},
                "model_id": 42,
                "shape": "not-a-shape",
            }
        ),
        "123456",
    )

    assert reading.oil_type == "Kerosene"
    assert reading.model_id == "42"
    assert reading.shape is None


def test_json_listing_skips_objects_without_an_id() -> None:
    assert parse_tank_ids('[{"name": "orphan"}, {"id": 123456}]') == ["123456"]


def test_json_listing_accepts_a_user_tanks_wrapper() -> None:
    assert parse_tank_ids('{"user_tanks": [{"id": 7}]}') == ["7"]


def test_json_with_a_bom_still_parses() -> None:
    assert parse_tank_ids('\ufeff{"tanks": []}') == []


def test_an_empty_account_page_is_not_a_javascript_shell() -> None:
    assert (
        parser.looks_like_javascript_shell(load_fixture("tanks_list_empty.html"))
        is False
    )
    assert parser.looks_like_javascript_shell('[{"id": 1}]') is False


def test_json_reads_a_reading_two_objects_down() -> None:
    reading = parse_tank_page(
        json.dumps(
            {
                "id": 123456,
                "monitor": {"latest_reading": {"percentage": 33, "litres": 800}},
            }
        ),
        "123456",
    )

    assert reading.level_percentage == 33
    assert reading.volume_litres == 800
