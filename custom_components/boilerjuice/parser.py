"""Turning BoilerJuice HTML into validated readings.

Pure functions: no network, no clock, no Home Assistant. Everything here
either produces a value that passed validation or leaves the field out.
"""

from __future__ import annotations

import json
import logging
import math
import re
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


def _bounded(value: Any, low: float, high: float, *, inclusive_low: bool = True):
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


def parse_tank_ids(html: str) -> list[str]:
    """Return the validated tank ids linked from the tanks listing page."""
    soup = BeautifulSoup(html, "html.parser")
    tank_ids: list[str] = []
    for link in soup.find_all("a", href=_TANK_LINK_RE):
        match = _TANK_LINK_RE.search(link["href"])
        if not match:
            continue
        tank_id = validate_tank_id(match.group(1))
        if tank_id is not None and tank_id not in tank_ids:
            tank_ids.append(tank_id)
    return tank_ids


def _first_input_value(soup: BeautifulSoup, element_ids: tuple[str, ...], convert):
    """Return the first of `element_ids` whose value survives `convert`."""
    for element_id in element_ids:
        element = soup.find("input", {"id": element_id})
        if element is None:
            continue
        converted = convert(element.get("value"))
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
    return percentage(oil_level.get("data-percentage"))


def _parse_volume(soup: BeautifulSoup) -> int | None:
    """Return the oil volume, which only appears as free text."""
    candidates = soup.find_all(
        string=lambda text: text
        and any(word in text.lower() for word in ["litre", "volume", "oil level"])
    )
    for text in candidates:
        lowered = text.strip().lower()
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
    select = soup.find("select", {"id": "tank_oil_type_id"})
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
    if element is None or not element.get("value"):
        return None, None, None

    model_id = element["value"]

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

        for entry in entries:
            if str(entry.get("id")) == str(model_id):
                tank = entry.get("tank", {})
                return model_id, tank.get("Description"), tank.get("Brand")
        break

    return model_id, None, None


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
        name=(
            element["value"]
            if (
                element := soup.find(
                    "input", {"id": "tank_user_tanks_attributes_0_name"}
                )
            )
            and element.get("value")
            else None
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
            "oil volume; refusing to treat it as a reading"
        )

    return reading
