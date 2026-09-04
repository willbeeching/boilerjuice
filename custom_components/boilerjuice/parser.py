"""Turning BoilerJuice HTML or JSON into validated readings.

Pure functions: no network, no clock, no Home Assistant. Everything here
either produces a value that passed validation or leaves the field out.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from typing import Any

from bs4 import BeautifulSoup

from .errors import BoilerJuiceAuthError, BoilerJuiceParseError
from .models import TankReading

_LOGGER = logging.getLogger(__name__)

# Sanity bounds for scraped values. A page that yields a number outside these
# is treated as a parse failure rather than a reading, so a layout change can
# never be mistaken for a real (and enormous) change in tank contents.
MAX_VOLUME_LITRES = 100_000
MAX_CAPACITY_LITRES = 100_000
MAX_HEIGHT_CM = 1_000
MAX_PRICE_PENCE = 1_000

_TANK_ID_RE = re.compile(r"^\d{1,12}$")
_TANK_LINK_RE = re.compile(r"/uk/users/tanks/(\d+)")
_OIL_VOLUME_RE = re.compile(r"(\d+)\s*litres?\s+(?:of\s+)?oil")
_PRICE_RE = re.compile(r"(\d+\.\d+)\s*pence per litre")

# Positive evidence that a tanks page really is showing an empty account.
# Each of these has to match a whole piece of text on the page, not appear
# somewhere inside one: "no tanks" as a substring turns a footer reading
# "No tanks need a delivery today" into proof that the account is empty,
# which then retires the very tanks the sentence was talking about.
#
# An invitation to add a tank is not on this list either: "Add another tank"
# belongs on a populated page.
_NO_TANKS_RE = re.compile(
    r"""
      (?:you\s+)?have\s+no\s+tanks?(?:\s+yet)?
    | (?:there\s+are\s+)?no\s+tanks?(?:\s+(?:yet|added|here|set\s+up|configured))?
    | you\s+have\s+not\s+added\s+(?:a|any)\s+tanks?(?:\s+yet)?
    | you\s+haven[\u2019']?t\s+added\s+(?:a|any)\s+tanks?(?:\s+yet)?
    | you\s+do\s*n[\u2019']?t\s+have\s+any\s+tanks?(?:\s+yet)?
    | add\s+your\s+first\s+tank
    """,
    re.I | re.VERBOSE,
)

# Punctuation and decoration a real page wraps such a message in.
_TRIM = " \t\r\n.!:\u2014-\u2022*"

# The elements a sentence is allowed to be assembled from. Matching against
# individual text nodes instead splits a sentence wherever it carries inline
# markup: <p><strong>No tanks</strong> need a delivery today</p> offers the
# fragment "No tanks" on its own, which is a match. Matching against block
# elements reassembles the sentence first. Inline tags are deliberately
# absent from this list, and so is the bare text of the page.
_BLOCK_TAGS = (
    "article",
    "aside",
    "blockquote",
    "body",
    "dd",
    "div",
    "dt",
    "figcaption",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "p",
    "section",
    "td",
    "th",
)

_SHAPES = ("cuboid", "horizontal_cylinder", "vertical_cylinder")


def finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or None if it is not one.

    NaN and the infinities are rejected: they survive arithmetic and would
    poison every stored total they touched.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _bounded(
    value: Any, low: float, high: float, *, inclusive_low: bool = True
) -> float | None:
    """Return a finite number inside the given bounds, or None."""
    number = finite(value)
    if number is None:
        return None
    if inclusive_low and not low <= number <= high:
        return None
    if not inclusive_low and not low < number <= high:
        return None
    return number


def percentage(value: Any) -> float | None:
    """Return a validated 0-100 percentage, or None."""
    return _bounded(value, 0, 100)


def volume_litres(value: Any) -> int | None:
    """Return a validated whole-litre volume, or None.

    The page only ever states whole litres, and the sensor state should read
    "1562", not "1562.0".
    """
    number = _bounded(value, 0, MAX_VOLUME_LITRES)
    return None if number is None else int(number)


def capacity_litres(value: Any) -> int | None:
    """Return a validated tank capacity in litres, or None.

    Zero is rejected as well as negatives: capacity is a divisor in the
    percentage-derived consumption path.
    """
    number = _bounded(value, 0, MAX_CAPACITY_LITRES, inclusive_low=False)
    return None if number is None else int(number)


def height_cm(value: Any) -> int | None:
    """Return a validated tank height in centimetres, or None."""
    number = _bounded(value, 0, MAX_HEIGHT_CM, inclusive_low=False)
    return None if number is None else int(number)


def price_pence(value: Any) -> float | None:
    """Return a validated price in pence per litre, or None."""
    return _bounded(value, 0, MAX_PRICE_PENCE, inclusive_low=False)


def validate_tank_id(value: Any) -> str | None:
    """Return ``value`` as a canonical numeric tank id, or None.

    BoilerJuice tank ids are the numeric path segment of
    ``/uk/users/tanks/<id>/edit``. Anything else would build a URL that
    silently 404s or, worse, resolves to a different page shape.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text if _TANK_ID_RE.match(text) else None


def _attribute(element: Any, name: str) -> str | None:
    """Return one HTML attribute as a plain string, or None.

    Beautiful Soup hands back a list for attributes it knows to be
    multi-valued, which no caller here wants.
    """
    if element is None:
        return None
    value = element.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        return " ".join(str(item) for item in value) or None
    return str(value)


def looks_like_login_page(html: str) -> bool:
    """Return True when ``html`` is the BoilerJuice sign-in page.

    Checks for the password field rather than the words "Sign in", which also
    appear in the signed-in navigation and made every successful login look
    like a failure the moment the site header changed.
    """
    return (
        BeautifulSoup(html, "html.parser").find("input", {"name": "user[password]"})
        is not None
    )


def parse_price(html: str) -> float | None:
    """Return the kerosene price in pence per litre, or None."""
    match = _PRICE_RE.search(html)
    if not match:
        return None
    return price_pence(match.group(1))


def looks_like_empty_tank_list(soup: BeautifulSoup) -> bool:
    """Return True when the page states that the account has no tanks.

    Each block element on the page has to say so on its own and say nothing
    else. Searching the page as a whole accepted "No tanks need a delivery
    today" - an ordinary status line on a populated page - as proof that the
    account was empty, and three such polls retire the tanks it was
    describing. Matching individual text nodes accepted the same sentence
    the moment two words of it were wrapped in <strong>.

    The document as a whole is a candidate too, which covers a page whose
    entire content is the message and nothing else.

    A block that holds the message plus anything else fails to match, and a
    tanks page nobody can read is a parse failure rather than an empty
    account, so the cost of being wrong here is a repair issue and not a
    deleted device.
    """
    candidates = [soup, *soup.find_all(_BLOCK_TAGS)]
    for element in candidates:
        message = " ".join(element.get_text(" ", strip=True).split()).strip(_TRIM)
        if message and _NO_TANKS_RE.fullmatch(message):
            return True
    return False


# Vendor strings that identify an interstitial rather than the page asked
# for. Matched case-insensitively against the document, and reported only
# as which one matched: a fixed list of our own words, never page content.
_INTERSTITIAL_MARKERS = {
    "cloudflare": ("cf-browser-verification", "cf-challenge", "just a moment"),
    "blocked": ("attention required", "access denied", "request blocked"),
    "rate_limited": ("too many requests", "rate limit"),
    "maintenance": ("under maintenance", "temporarily unavailable"),
}


def describe_page_shape(html: str) -> dict[str, Any]:
    """Describe an unexpected page without reproducing any of it.

    A page we cannot read is invisible by design: page HTML is never
    logged, at any level, because it carries the account. That left a
    layout change, a Cloudflare challenge and a maintenance page looking
    identical from the outside, which is no way to diagnose an install
    you cannot reach.

    Everything below is a count, a boolean, or one of our own fixed
    words. No text from the page is copied into the result.
    """
    lowered = html.lower()
    soup = BeautifulSoup(html, "html.parser")

    interstitial = sorted(
        name
        for name, markers in _INTERSTITIAL_MARKERS.items()
        if any(marker in lowered for marker in markers)
    )

    return {
        "bytes": len(html),
        "is_html": soup.find("html") is not None or soup.find("body") is not None,
        "forms": len(soup.find_all("form")),
        "password_inputs": len(soup.find_all("input", {"type": "password"})),
        "links": len(soup.find_all("a")),
        "tank_links": len(soup.find_all("a", href=_TANK_LINK_RE)),
        "scripts": len(soup.find_all("script")),
        "looks_like_interstitial": interstitial,
    }


def parse_tank_ids(html: str) -> list[str]:
    """Return the validated tank ids linked from the tanks listing page.

    An empty list means the account really has no tanks, and only that.
    A page with no recognised tank links and no recognisable empty state
    raises BoilerJuiceParseError instead, because "we no longer understand
    this page" and "you have no tanks" are not the same fact, and the
    coordinator acts on the second by removing devices.

    This is the same mistake the tank page used to make, one page earlier:
    treating an unreadable response as a confident measurement of nothing.

    The listing is now a JavaScript app that fetches JSON. A body that
    looks like JSON is read as JSON; HTML is still accepted so an older
    page, or a test fixture, keeps working.
    """
    if looks_like_json(html):
        return _parse_tank_ids_json(html)

    soup = BeautifulSoup(html, "html.parser")
    tank_ids: list[str] = []
    for link in soup.find_all("a", href=_TANK_LINK_RE):
        href = _attribute(link, "href")
        match = None if href is None else _TANK_LINK_RE.search(href)
        if not match:
            continue
        tank_id = validate_tank_id(match.group(1))
        if tank_id is not None and tank_id not in tank_ids:
            tank_ids.append(tank_id)

    if tank_ids or looks_like_empty_tank_list(soup):
        return tank_ids

    # The shape goes in the message so the reason a user reads in the UI
    # says what kind of page arrived, not merely that one did.
    raise BoilerJuiceParseError(
        "The BoilerJuice tanks page listed no tanks and did not look like an "
        "empty account; refusing to treat it as proof that the tanks are gone "
        f"(page shape: {describe_page_shape(html)})"
    )


# Keys the JSON API has used, or that a Rails-style tank object is likely
# to use. Tried in order; the first value that survives validation wins.
# A renamed field that is not on this list costs a parse error with the
# JSON shape in the message, not a guessed zero.
_JSON_ID_KEYS = ("id", "tank_id")
_JSON_LEVEL_KEYS = (
    "level_percentage",
    "oil_level_percentage",
    "usable_level_percentage",
    "total_level_percentage",
    "usable_percentage",
    "total_percentage",
    "percentage",
    "percent",
    "oil_level",
    "level",
)
_JSON_VOLUME_KEYS = (
    "volume_litres",
    "current_volume_litres",
    "usable_volume_litres",
    "remaining_litres",
    "oil_volume",
    "litres",
    "liters",
    "volume",
)
_JSON_CAPACITY_KEYS = ("capacity_litres", "tank_size", "capacity", "size")
_JSON_HEIGHT_KEYS = ("internal_height", "height_cm", "height")
_JSON_NAME_KEYS = ("name", "tank_name")
_JSON_SHAPE_KEYS = ("shape", "tank_shape")
_JSON_OIL_TYPE_KEYS = ("oil_type", "oilType")
_JSON_MODEL_KEYS = ("model", "Description", "description")
_JSON_MANUFACTURER_KEYS = ("manufacturer", "Brand", "brand")
_JSON_MODEL_ID_KEYS = ("model_id", "tank_model_id")
_JSON_LIST_KEYS = ("tanks", "user_tanks", "data", "results")

# Field names we already understand. The shape reporter may name these
# and nothing else: an unexpected payload can use a tank id or an email
# address as a key, and those must not reach a log or a diagnostic.
_RECOGNISED_JSON_KEYS = frozenset(
    (
        *_JSON_ID_KEYS,
        *_JSON_LEVEL_KEYS,
        *_JSON_VOLUME_KEYS,
        *_JSON_CAPACITY_KEYS,
        *_JSON_HEIGHT_KEYS,
        *_JSON_NAME_KEYS,
        *_JSON_SHAPE_KEYS,
        *_JSON_OIL_TYPE_KEYS,
        *_JSON_MODEL_KEYS,
        *_JSON_MANUFACTURER_KEYS,
        *_JSON_MODEL_ID_KEYS,
        *_JSON_LIST_KEYS,
        "error",
    )
)
_MAX_SHAPE_KEYS = 32


def _strip_json_prefix(body: str) -> str:
    """Remove a UTF-8 BOM and leading whitespace so JSON parsers agree."""
    return body.lstrip("\ufeff \t\r\n")


def looks_like_json(body: str) -> bool:
    """Return True when `body` is more likely JSON than HTML."""
    stripped = _strip_json_prefix(body)
    return stripped.startswith("{") or stripped.startswith("[")


def looks_like_javascript_shell(html: str) -> bool:
    """Return True when `html` is a JS app shell with no tank markup.

    The tanks page now ships as scripts and an empty body. That is not an
    empty account and it is not a login page; the tanks are on the JSON
    API behind the same path.
    """
    if looks_like_json(html) or looks_like_login_page(html):
        return False
    soup = BeautifulSoup(html, "html.parser")
    if looks_like_empty_tank_list(soup):
        return False
    shape = describe_page_shape(html)
    return bool(
        shape["is_html"]
        and shape["scripts"] > 0
        and shape["tank_links"] == 0
        and shape["forms"] == 0
    )


def _load_json(body: str) -> Any:
    """Parse `body` as JSON, or raise a parse error that names no content."""
    try:
        return json.loads(_strip_json_prefix(body))
    except json.JSONDecodeError:
        raise BoilerJuiceParseError(
            "The BoilerJuice response looked like JSON but did not parse"
        ) from None


def _json_type(value: Any) -> str:
    """Return a JSON type name, never the value itself."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "other"


def _recognised_key_names(keys: Any) -> list[str]:
    """Return the allowlisted names in `keys`, sorted and capped."""
    names = sorted({str(key) for key in keys if str(key) in _RECOGNISED_JSON_KEYS})
    return names[:_MAX_SHAPE_KEYS]


def describe_json_shape(data: Any) -> dict[str, Any]:
    """Describe a JSON payload without reproducing any of its content.

    Safe to log and to paste into an issue: counts, JSON types, and a
    capped allowlist of field names we already recognise. Arbitrary
    keys are not copied; they can be tank ids or email addresses.
    """
    if isinstance(data, list):
        keys: set[str] = set()
        for item in data:
            if isinstance(item, dict):
                keys.update(str(key) for key in item)
        return {
            "is_json": True,
            "type": "list",
            "length": len(data),
            "item_key_count": len(keys),
            "recognised_item_keys": _recognised_key_names(keys),
        }
    if isinstance(data, dict):
        recognised = _recognised_key_names(data)
        nested: dict[str, str] = {}
        for key, value in data.items():
            name = str(key)
            if name in recognised:
                nested[name] = _json_type(value)
        return {
            "is_json": True,
            "type": "object",
            "key_count": len(data),
            "recognised_keys": recognised,
            "nested": nested,
        }
    return {"is_json": True, "type": _json_type(data)}


def looks_like_json_sign_in(body: str) -> bool:
    """Return True when a JSON body is Devise's "please sign in" error."""
    if not looks_like_json(body):
        return False
    try:
        data = json.loads(_strip_json_prefix(body))
    except json.JSONDecodeError:
        return False
    return _json_says_sign_in(data)


def _json_says_sign_in(data: Any) -> bool:
    """Return True when `data` is the unauthenticated JSON error."""
    if not isinstance(data, dict):
        return False
    error = data.get("error")
    return isinstance(error, str) and "sign in" in error.lower()


def _tank_objects(data: Any) -> list[dict[str, Any]] | None:
    """Return the tank objects in `data`, or None if the shape is unknown.

    An empty list means the payload is a recognised tank list with nothing
    in it. None means we do not understand the payload, which is a parse
    failure rather than an empty account.
    """
    if isinstance(data, list):
        if all(isinstance(item, dict) for item in data):
            return list(data)
        return None
    if not isinstance(data, dict):
        return None
    for key in _JSON_LIST_KEYS:
        value = data.get(key)
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return list(value)
    if any(key in data for key in _JSON_ID_KEYS):
        return [data]
    return None


def _collect_dicts(obj: dict[str, Any], *, depth: int = 0) -> list[dict[str, Any]]:
    """Return `obj` and its nested objects, two levels down.

    Readings sometimes live on a child (`latest_reading`, `monitor`) rather
    than on the tank itself. Deeper walking would start inventing values
    from unrelated nested documents.
    """
    found = [obj]
    if depth >= 2:
        return found
    for value in obj.values():
        if isinstance(value, dict):
            found.extend(_collect_dicts(value, depth=depth + 1))
    return found


def _first_converted(
    objects: list[dict[str, Any]],
    keys: tuple[str, ...],
    convert: Callable[[Any], Any],
) -> Any:
    """Return the first value under `keys` that survives `convert`."""
    for obj in objects:
        for key in keys:
            if key not in obj:
                continue
            converted = convert(obj[key])
            if converted is not None:
                return converted
    return None


def _first_raw(objects: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    """Return the first present value under `keys`, unconverted."""
    for obj in objects:
        for key in keys:
            if key in obj:
                return obj[key]
    return None


def _has_tank_id_key(obj: dict[str, Any]) -> bool:
    """Return True when a tank-id field is present, even if unreadable."""
    return any(key in obj for key in _JSON_ID_KEYS)


def _shape_from_json(value: Any) -> str | None:
    """Return a known tank shape, formatted the way the HTML parser does."""
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace(" ", "_")
    if key not in _SHAPES:
        return None
    return key.replace("_", " ").title()


def _oil_type_from_json(value: Any) -> str | None:
    """Return an oil type name from a string or a small object."""
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("label"))
    return None


def _model_id_from_json(value: Any) -> str | None:
    """Return a model id as a string, or None if there is not one."""
    if value is None or value == "":
        return None
    return _text(str(value))


def _parse_tank_ids_json(body: str) -> list[str]:
    """Return tank ids from a JSON listing, or raise."""
    data = _load_json(body)
    if _json_says_sign_in(data):
        raise BoilerJuiceAuthError(
            "BoilerJuice served the sign-in error; the session expired or the "
            "credentials were rejected"
        )
    objects = _tank_objects(data)
    if objects is None:
        raise BoilerJuiceParseError(
            "The BoilerJuice tanks JSON listed no tanks and was not an empty "
            f"list; refusing to treat it as proof that the tanks are gone "
            f"(page shape: {describe_json_shape(data)})"
        )
    tank_ids: list[str] = []
    unreadable = False
    for obj in objects:
        tank_id = validate_tank_id(_first_raw([obj], _JSON_ID_KEYS))
        if tank_id is None:
            # Track this apart from deduplication. A sibling we cannot
            # name is not "absent"; treating the rest as complete would
            # retire that tank after three polls.
            unreadable = True
            continue
        if tank_id not in tank_ids:
            tank_ids.append(tank_id)
    if unreadable:
        raise BoilerJuiceParseError(
            "The BoilerJuice tanks JSON listed objects that could not all "
            "be identified; refusing to treat the listing as complete "
            f"(page shape: {describe_json_shape(data)})"
        )
    return tank_ids


def _parse_tank_json(body: str, tank_id: str) -> TankReading:
    """Parse a JSON tank document into a validated reading."""
    canonical = validate_tank_id(tank_id)
    if canonical is None:
        raise BoilerJuiceParseError("Refusing to use a non-numeric tank id")

    data = _load_json(body)
    if _json_says_sign_in(data):
        raise BoilerJuiceAuthError(
            "BoilerJuice served the sign-in error; the session expired or the "
            "credentials were rejected"
        )
    objects = _tank_objects(data)
    if objects is None:
        raise BoilerJuiceParseError(
            "The BoilerJuice tank JSON was not a tank object "
            f"(page shape: {describe_json_shape(data)})"
        )
    matches = [
        obj
        for obj in objects
        if validate_tank_id(_first_raw([obj], _JSON_ID_KEYS)) == canonical
    ]
    if matches:
        chosen = matches[0]
    elif len(objects) == 1 and not _has_tank_id_key(objects[0]):
        # A singleton with no id field is the document we asked for.
        # An explicit id that is invalid or names another tank is not.
        chosen = objects[0]
    else:
        raise BoilerJuiceParseError(
            "The BoilerJuice tank JSON listed tanks but not the one "
            f"requested (page shape: {describe_json_shape(data)})"
        )

    fields = _collect_dicts(chosen)
    reading = TankReading(
        tank_id=canonical,
        level_percentage=_first_converted(fields, _JSON_LEVEL_KEYS, percentage),
        volume_litres=_first_converted(fields, _JSON_VOLUME_KEYS, volume_litres),
        capacity_litres=_first_converted(fields, _JSON_CAPACITY_KEYS, capacity_litres),
        height_cm=_first_converted(fields, _JSON_HEIGHT_KEYS, height_cm),
        name=_first_converted(fields, _JSON_NAME_KEYS, _text),
        model=_first_converted(fields, _JSON_MODEL_KEYS, _text),
        model_id=_first_converted(fields, _JSON_MODEL_ID_KEYS, _model_id_from_json),
        manufacturer=_first_converted(fields, _JSON_MANUFACTURER_KEYS, _text),
        shape=_first_converted(fields, _JSON_SHAPE_KEYS, _shape_from_json),
        oil_type=_first_converted(fields, _JSON_OIL_TYPE_KEYS, _oil_type_from_json),
    )
    if not reading.has_measurement:
        raise BoilerJuiceParseError(
            "The BoilerJuice tank JSON contained neither an oil level nor an "
            f"oil volume; refusing to treat it as a reading "
            f"(page shape: {describe_json_shape(data)})"
        )
    return reading


def _first_input_value(
    soup: BeautifulSoup,
    element_ids: tuple[str, ...],
    convert: Callable[[Any], int | None],
) -> int | None:
    """Return the first of `element_ids` whose value survives `convert`."""
    for element_id in element_ids:
        element = soup.find("input", {"id": element_id})
        if element is None:
            continue
        converted = convert(_attribute(element, "value"))
        if converted is not None:
            return converted
    return None


def _parse_level(soup: BeautifulSoup) -> float | None:
    """Return the oil level percentage shown on the page."""
    container = soup.find("div", {"id": "usable-oil"})
    if container is None:
        return None
    oil_level = container.find("div", {"class": "oil-level"})
    if oil_level is None:
        return None
    return percentage(_attribute(oil_level, "data-percentage"))


def _parse_volume(soup: BeautifulSoup) -> int | None:
    """Return the oil volume, which only appears as free text."""
    candidates = soup.find_all(
        string=lambda text: (
            text
            and any(word in text.lower() for word in ["litre", "volume", "oil level"])
        )
    )
    for text in candidates:
        lowered = str(text).strip().lower()
        if "litres of oil" not in lowered and "litres oil" not in lowered:
            continue
        match = _OIL_VOLUME_RE.search(lowered)
        if match:
            found = volume_litres(match.group(1))
            if found is not None:
                return found
    return None


def _parse_shape(soup: BeautifulSoup) -> str | None:
    """Return the selected tank shape."""
    for shape in _SHAPES:
        element = soup.find(
            "input", {"type": "radio", "name": "tank-shape", "value": shape}
        )
        # `has_attr`, not `get`: a bare `checked` attribute parses as an empty
        # string, so `get` reported every shape as unselected.
        if element is not None and element.has_attr("checked"):
            return shape.replace("_", " ").title()
    return None


def _parse_oil_type(soup: BeautifulSoup) -> str | None:
    """Return the selected oil type."""
    select = soup.find("select", {"id": "tank_oil_type_id"}) or soup.find(
        "select", {"id": "oil_type_id"}
    )
    if select is None:
        return None
    selected = select.find("option", selected=True)
    return selected.text if selected else None


def _extract_json_array(script_text: str) -> str | None:
    """Return the `var jsonData = [...]` array source, or None."""
    array_start = script_text.find("[", script_text.find("var jsonData = "))
    if array_start < 0:
        return None

    depth = 1
    index = array_start + 1
    while index < len(script_text) and depth > 0:
        if script_text[index] == "[":
            depth += 1
        elif script_text[index] == "]":
            depth -= 1
        index += 1

    return None if depth else script_text[array_start:index]


def _parse_model(soup: BeautifulSoup) -> tuple[str | None, str | None, str | None]:
    """Return (model_id, model, manufacturer) from the page's inline JSON."""
    element = soup.find("input", {"id": "tankModelInput"})
    model_id = _attribute(element, "value")
    if model_id is None:
        return None, None, None

    for script in soup.find_all("script"):
        if not (script.string and "var jsonData = " in script.string):
            continue

        source = _extract_json_array(script.string)
        if source is None:
            break
        try:
            entries = json.loads(source)
        except json.JSONDecodeError as err:
            _LOGGER.debug("Tank model JSON did not parse: %s", err)
            break

        # Shape-checked at every step. The blob is optional decoration, so a
        # malformed one has to cost the model name and nothing else; walking
        # it on trust raised AttributeError out of the parser instead, and
        # took the whole tank reading with it.
        if not isinstance(entries, list):
            _LOGGER.debug("Tank model JSON was not a list of models")
            break

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id")) != str(model_id):
                continue
            tank = entry.get("tank")
            if not isinstance(tank, dict):
                break
            return model_id, _text(tank.get("Description")), _text(tank.get("Brand"))
        break

    return model_id, None, None


def _text(value: Any) -> str | None:
    """Return a non-empty string, or None for anything else.

    A model name is shown to the user and stored on the device, so a number
    or a nested object where a name should be is dropped rather than
    stringified into something like "{'en': 'Titan'}".
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def parse_tank_page(html: str, tank_id: str) -> TankReading:
    """Parse a tank edit page into a validated reading.

    Raises BoilerJuiceParseError unless the page yields a numeric tank id
    plus at least one usable reading source, so a truncated response or a
    redesigned page cannot be accepted as "the tank is empty".
    """
    if looks_like_login_page(html):
        raise BoilerJuiceAuthError(
            "BoilerJuice served the sign-in page; the session expired or the "
            "credentials were rejected"
        )
    if looks_like_json(html):
        return _parse_tank_json(html, tank_id)

    canonical_tank_id = validate_tank_id(tank_id)
    if canonical_tank_id is None:
        raise BoilerJuiceParseError("Refusing to use a non-numeric tank id")

    soup = BeautifulSoup(html, "html.parser")
    model_id, model, manufacturer = _parse_model(soup)

    reading = TankReading(
        tank_id=canonical_tank_id,
        level_percentage=_parse_level(soup),
        volume_litres=_parse_volume(soup),
        # The ids changed from 'tank-size-count' / 'tank-height-count'.
        capacity_litres=_first_input_value(
            soup, ("tank_size", "tank-size-count"), capacity_litres
        ),
        height_cm=_first_input_value(
            soup, ("internal_height", "tank-height-count"), height_cm
        ),
        name=_attribute(
            soup.find("input", {"id": "tank_user_tanks_attributes_0_name"})
            or soup.find("input", {"id": "name", "name": "name"}),
            "value",
        ),
        model=model,
        model_id=model_id,
        manufacturer=manufacturer,
        shape=_parse_shape(soup),
        oil_type=_parse_oil_type(soup),
    )

    # A reading is only usable if at least one of the two consumption sources
    # survived validation. Without this a redesigned page parses to "nothing
    # found", which the old code turned into 0 L / 0% and booked as a drop of
    # the entire tank.
    if not reading.has_measurement:
        raise BoilerJuiceParseError(
            "The BoilerJuice tank page contained neither an oil level nor an "
            f"oil volume; refusing to treat it as a reading "
            f"(page shape: {describe_page_shape(html)})"
        )

    return reading
