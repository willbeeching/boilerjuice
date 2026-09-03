"""Data update coordinator for BoilerJuice."""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple, Union

import aiohttp
from bs4 import BeautifulSoup
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EMAIL,
    CONF_KWH_PER_LITRE,
    CONF_PASSWORD,
    CONF_TANK_ID,
    DEFAULT_KWH_PER_LITRE,
    DOMAIN,
    LOGIN_URL,
    PRICE_URL,
    TANKS_URL,
)

_LOGGER = logging.getLogger(__name__)

# Update every hour to allow smooth accumulation of energy consumption
SCAN_INTERVAL = timedelta(hours=1)

# Number of days to keep in rolling average
CONSUMPTION_ROLLING_DAYS = 7

# Seasonal tracking constants
# Dated history is kept for just over a year so every season has data to
# average. Entries are collapsed to one per day, so this bounds the stored
# history at ~400 rows.
SEASONAL_HISTORY_DAYS = 400
WINTER_MONTHS = [12, 1, 2]
SPRING_MONTHS = [3, 4, 5]
SUMMER_MONTHS = [6, 7, 8]
AUTUMN_MONTHS = [9, 10, 11]

# Storage constants
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_consumption_data"

# Every request gets an explicit budget. Without one aiohttp waits forever, so
# a stalled BoilerJuice response would pin the coordinator open until Home
# Assistant restarted.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(
    total=45, connect=10, sock_connect=10, sock_read=20
)

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


class BoilerJuiceAuthError(UpdateFailed):
    """BoilerJuice rejected the configured credentials."""


class BoilerJuiceConnectionError(UpdateFailed):
    """BoilerJuice could not be reached or the login flow could not be driven."""


class BoilerJuiceParseError(UpdateFailed):
    """A BoilerJuice page did not yield a usable tank reading.

    Raised instead of returning a partially-filled reading. A truncated page,
    a login redirect or a site redesign all land here, and the coordinator
    keeps its previous state rather than recording a phantom drop to zero.
    """


def _finite(value: Any) -> float | None:
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


def _percentage(value: Any) -> float | None:
    """Return a validated 0-100 percentage, or None."""
    number = _finite(value)
    if number is None or not 0 <= number <= 100:
        return None
    return number


def _volume_litres(value: Any) -> float | None:
    """Return a validated volume in litres, or None."""
    number = _finite(value)
    if number is None or not 0 <= number <= MAX_VOLUME_LITRES:
        return None
    return number


def _capacity_litres(value: Any) -> int | None:
    """Return a validated tank capacity in litres, or None.

    Zero is rejected as well as negatives: capacity is a divisor in the
    percentage-derived consumption path.
    """
    number = _finite(value)
    if number is None or not 0 < number <= MAX_CAPACITY_LITRES:
        return None
    return int(number)


def _height_cm(value: Any) -> int | None:
    """Return a validated tank height in centimetres, or None."""
    number = _finite(value)
    if number is None or not 0 < number <= MAX_HEIGHT_CM:
        return None
    return int(number)


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
    soup = BeautifulSoup(html, "html.parser")
    return soup.find("input", {"name": "user[password]"}) is not None


def _parse_tank_model(soup: BeautifulSoup, data: Dict[str, Any]) -> None:
    """Pull the tank model and manufacturer out of the page's inline JSON."""
    tank_model_input = soup.find("input", {"id": "tankModelInput"})
    if not (tank_model_input and tank_model_input.get("value")):
        _LOGGER.debug("Could not find tank model ID")
        return

    model_id = tank_model_input.get("value")
    data["model_id"] = model_id

    for script in soup.find_all("script"):
        if not (script.string and "var jsonData = " in script.string):
            continue

        script_text = script.string
        array_start = script_text.find("[", script_text.find("var jsonData = "))
        if array_start < 0:
            break

        bracket_count = 1
        array_end = array_start + 1
        while array_end < len(script_text) and bracket_count > 0:
            if script_text[array_end] == "[":
                bracket_count += 1
            elif script_text[array_end] == "]":
                bracket_count -= 1
            array_end += 1

        if bracket_count != 0:
            break

        try:
            json_data = json.loads(script_text[array_start:array_end])
        except json.JSONDecodeError as err:
            _LOGGER.debug("Failed to parse tank model JSON: %s", err)
            break

        for item in json_data:
            if str(item.get("id")) == str(model_id):
                data["model"] = item.get("tank", {}).get("Description")
                data["manufacturer"] = item.get("tank", {}).get("Brand")
                break
        break


def parse_tank_page(html: str, tank_id: str) -> Dict[str, Any]:
    """Parse a tank edit page into a validated reading.

    Only fields that parsed cleanly are present in the result: a missing
    percentage or volume is absent, never zero. Raises BoilerJuiceParseError
    unless the page yields a tank id plus at least one usable reading source,
    so a login redirect, a truncated response or a redesigned page cannot be
    accepted as "the tank is empty".
    """
    if looks_like_login_page(html):
        raise BoilerJuiceAuthError(
            "BoilerJuice redirected to the sign-in page; the session expired "
            "or the credentials were rejected"
        )

    canonical_tank_id = validate_tank_id(tank_id)
    if canonical_tank_id is None:
        raise BoilerJuiceParseError(
            f"Refusing to use a non-numeric tank id: {tank_id!r}"
        )

    soup = BeautifulSoup(html, "html.parser")
    data: Dict[str, Any] = {}

    # Oil level. BoilerJuice now publishes a single level ("total oil
    # remaining") where it used to expose separate total and usable figures.
    usable_level_div = soup.find("div", {"id": "usable-oil"})
    if usable_level_div:
        oil_level = usable_level_div.find("div", {"class": "oil-level"})
        if oil_level:
            level_percent = _percentage(oil_level.get("data-percentage"))
            if level_percent is not None:
                data["total_level_percentage"] = level_percent
                data["usable_level_percentage"] = level_percent

    # Tank capacity. The id changed from 'tank-size-count' to 'tank_size'.
    for element_id in ("tank_size", "tank-size-count"):
        tank_size_input = soup.find("input", {"id": element_id})
        if tank_size_input:
            capacity = _capacity_litres(tank_size_input.get("value"))
            if capacity is not None:
                data["capacity_litres"] = capacity
                break

    # Tank height. The id changed from 'tank-height-count' to 'internal_height'.
    for element_id in ("internal_height", "tank-height-count"):
        tank_height_input = soup.find("input", {"id": element_id})
        if tank_height_input:
            height = _height_cm(tank_height_input.get("value"))
            if height is not None:
                data["height_cm"] = height
                break

    # Oil volume, which only appears as free text on the page.
    volume_texts = soup.find_all(
        string=lambda text: text
        and any(word in text.lower() for word in ["litre", "volume", "oil level"])
    )
    for text in volume_texts:
        lowered = text.strip().lower()
        if "litres of oil" not in lowered and "litres oil" not in lowered:
            continue
        match = _OIL_VOLUME_RE.search(lowered)
        if not match:
            continue
        volume = _volume_litres(match.group(1))
        if volume is not None:
            data["current_volume_litres"] = volume
            data["usable_volume_litres"] = volume
            break

    tank_name_input = soup.find("input", {"id": "tank_user_tanks_attributes_0_name"})
    if tank_name_input and tank_name_input.get("value"):
        data["name"] = tank_name_input["value"]

    _parse_tank_model(soup, data)

    for shape in ["cuboid", "horizontal_cylinder", "vertical_cylinder"]:
        shape_input = soup.find(
            "input", {"type": "radio", "name": "tank-shape", "value": shape}
        )
        # `has_attr`, not `get`: a bare `checked` attribute parses as an empty
        # string, so `get` reported every shape as unselected.
        if shape_input and shape_input.has_attr("checked"):
            data["shape"] = shape.replace("_", " ").title()
            break

    oil_type_select = soup.find("select", {"id": "tank_oil_type_id"})
    if oil_type_select:
        selected_option = oil_type_select.find("option", selected=True)
        if selected_option:
            data["oil_type"] = selected_option.text

    # A reading is only usable if at least one of the two consumption sources
    # survived validation. Without this a redesigned page parses to "nothing
    # found", which the old code turned into 0 L / 0% and booked as a drop of
    # the entire tank.
    if (
        data.get("total_level_percentage") is None
        and data.get("usable_volume_litres") is None
    ):
        raise BoilerJuiceParseError(
            "The BoilerJuice tank page contained neither an oil level nor an "
            "oil volume; refusing to treat it as a reading"
        )

    data["id"] = canonical_tank_id
    return data


def parse_tank_ids(html: str) -> List[str]:
    """Return the validated tank ids linked from the tanks listing page."""
    soup = BeautifulSoup(html, "html.parser")
    tank_ids: List[str] = []
    for link in soup.find_all("a", href=_TANK_LINK_RE):
        match = _TANK_LINK_RE.search(link["href"])
        if not match:
            continue
        tank_id = validate_tank_id(match.group(1))
        if tank_id is not None and tank_id not in tank_ids:
            tank_ids.append(tank_id)
    return tank_ids


class BoilerJuiceDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching BoilerJuice data."""

    def __init__(
        self, hass: HomeAssistant, config: Union[ConfigEntry, Dict[str, Any]]
    ) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self._config = config
        # Each coordinator owns its own aiohttp session with a dedicated cookie
        # jar. Using the shared HA session caused two BoilerJuice accounts to
        # overwrite each other's login cookies (see GitHub issue #3).
        self._session: aiohttp.ClientSession | None = None
        self._previous_usable_volume = None
        self._previous_total_level = None
        self._total_consumption_usable_liters = 0.0
        self._total_consumption_usable_kwh = 0.0
        self._daily_consumption_usable_liters = 0.0
        self._last_update = None
        self._kwh_per_litre = self._validated_kwh_per_litre()
        # Add list to store daily consumption history
        self._daily_consumption_history = []
        # Add seasonal tracking
        self._consumption_history_with_dates: List[Tuple[datetime, float]] = []
        # The oil price comes from a separate, optional request. Keep the last
        # good value so one failed fetch does not blank the price sensors.
        self._last_price_pence: float | None = None
        self._last_price_updated: datetime | None = None

        # Set up storage. Keyed per config entry so multiple accounts don't
        # collide on the legacy "default" bucket when no tank id is provided.
        self._entry_id: str | None = (
            config.entry_id if isinstance(config, ConfigEntry) else None
        )
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._tank_id = validate_tank_id(self._get_config_value_optional(CONF_TANK_ID))
        if self._tank_id is None and self._get_config_value_optional(CONF_TANK_ID):
            _LOGGER.warning(
                "Ignoring the configured BoilerJuice tank id because it is not "
                "numeric; the first tank on the account will be used instead"
            )

        # Flag to track if data has been loaded
        self._consumption_data_loaded = False

    def _validated_kwh_per_litre(self) -> float:
        """Return the configured energy content, falling back to the default."""
        configured = _finite(
            self._get_config_value_optional(CONF_KWH_PER_LITRE, DEFAULT_KWH_PER_LITRE)
        )
        if configured is None or configured <= 0:
            _LOGGER.warning(
                "Configured kWh per litre is not a positive number; using %s",
                DEFAULT_KWH_PER_LITRE,
            )
            return DEFAULT_KWH_PER_LITRE
        return configured

    def _get_config_value(self, key: str) -> Any:
        """Get a configuration value, handling both ConfigEntry and dict inputs."""
        if isinstance(self._config, ConfigEntry):
            return self._config.data[key]
        return self._config[key]

    def _get_config_value_optional(self, key: str, default: Any = None) -> Any:
        """Get an optional configuration value, handling both ConfigEntry and dict inputs."""
        if isinstance(self._config, ConfigEntry):
            return self._config.data.get(key, default)
        return self._config.get(key, default)

    @property
    def kwh_per_litre(self) -> float:
        """Return the configured energy content of a litre of oil."""
        return self._kwh_per_litre

    @property
    def total_consumption_usable_liters(self) -> float:
        """Return the total oil consumption in liters."""
        return self._total_consumption_usable_liters

    @property
    def total_consumption_usable_kwh(self) -> float:
        """Return the total oil consumption in kWh."""
        return self._total_consumption_usable_kwh

    @property
    def daily_consumption_usable_liters(self) -> float:
        """Return the average daily oil consumption in liters."""
        return self._daily_consumption_usable_liters

    @staticmethod
    def _as_local(value: datetime) -> datetime:
        """Return `value` as a timezone-aware local datetime.

        Timestamps written before this integration became timezone-aware were
        naive local wall-clock (plain `datetime.now()`), so they are localized
        rather than reinterpreted as UTC. Without this, stored history would
        mix naive and aware values and every comparison would raise TypeError.
        """
        if value.tzinfo is None:
            return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return dt_util.as_local(value)

    @staticmethod
    def _local_midnight(date_str: str) -> datetime:
        """Return local midnight for an ISO date key from the daily totals."""
        return datetime.fromisoformat(date_str).replace(
            tzinfo=dt_util.DEFAULT_TIME_ZONE
        )

    def _apply_stored_data(self, source: str, data: dict) -> None:
        """Hydrate coordinator state from a stored-data blob."""
        self._total_consumption_usable_liters = data.get(
            "total_consumption_liters", 0.0
        )
        self._total_consumption_usable_kwh = data.get("total_consumption_kwh", 0.0)
        self._daily_consumption_usable_liters = data.get(
            "daily_consumption_liters", 0.0
        )
        self._daily_consumption_history = data.get("consumption_history", [])

        history_with_dates = data.get("consumption_history_with_dates", [])
        self._consumption_history_with_dates = [
            (self._as_local(datetime.fromisoformat(dt)), cons)
            for dt, cons in history_with_dates
        ]

        last_update_str = data.get("last_update")
        if last_update_str:
            try:
                self._last_update = self._as_local(
                    datetime.fromisoformat(last_update_str)
                )
            except (ValueError, TypeError):
                self._last_update = None

        self._previous_usable_volume = data.get("reference_volume")
        self._previous_total_level = data.get("reference_level")

        _LOGGER.info(
            "Loaded stored consumption data from %s: total=%s L, daily=%s L/day",
            source,
            self._total_consumption_usable_liters,
            self._daily_consumption_usable_liters,
        )

    async def _load_consumption_data(self) -> None:
        """Load consumption data from storage.

        Data is keyed by config entry id so that multiple BoilerJuice accounts
        never share state. For backwards compatibility we fall back to the
        legacy tank-id key (and, only when a tank id is configured, the
        "default" key) so existing users don't lose consumption history on
        upgrade.
        """
        if self._consumption_data_loaded:
            return

        stored_data = await self._store.async_load() or {}

        loaded = False

        if self._entry_id and self._entry_id in stored_data:
            self._apply_stored_data(
                f"entry {self._entry_id}", stored_data[self._entry_id]
            )
            loaded = True
        elif self._tank_id and self._tank_id in stored_data:
            # Legacy per-tank key - migrate into the entry-keyed slot.
            self._apply_stored_data(
                f"legacy tank {self._tank_id}", stored_data[self._tank_id]
            )
            loaded = True
        elif self._tank_id and stored_data.get("default"):
            # Only migrate the legacy "default" bucket when we can be sure it
            # belongs to this entry (i.e. a tank id is explicitly configured).
            # With multiple untagged accounts the default bucket is ambiguous,
            # so we leave it untouched rather than risk cross-contamination.
            self._apply_stored_data("legacy default", stored_data["default"])
            loaded = True

        if not loaded and stored_data:
            _LOGGER.debug(
                "No stored consumption data for entry %s; starting fresh",
                self._entry_id,
            )

        self._consumption_data_loaded = True

    def _get_season(self, date: datetime) -> str:
        """Get the season for a given date."""
        month = date.month
        if month in WINTER_MONTHS:
            return "winter"
        elif month in SPRING_MONTHS:
            return "spring"
        elif month in SUMMER_MONTHS:
            return "summer"
        else:
            return "autumn"

    def _calculate_daily_totals_from_history(self) -> Dict[str, float]:
        """Group consumption history by date and return daily totals."""
        daily_totals: Dict[str, float] = {}

        for dt, consumption in self._consumption_history_with_dates:
            date_key = dt.date().isoformat()
            if date_key in daily_totals:
                daily_totals[date_key] += consumption
            else:
                daily_totals[date_key] = consumption

        return dict(sorted(daily_totals.items()))

    def _calculate_seasonal_stats(self) -> Dict[str, Any]:
        """Calculate seasonal consumption statistics."""
        if not self._consumption_history_with_dates:
            return {}

        # Get daily totals first to avoid double-counting same-day updates
        daily_totals = self._calculate_daily_totals_from_history()

        if not daily_totals:
            return {}

        # Initialize seasonal data
        seasonal_data: Dict[str, Any] = {
            "winter": [],
            "spring": [],
            "summer": [],
            "autumn": [],
            "monthly": {},
            "current_season": {"name": "", "avg": 0.0, "min": 0.0, "max": 0.0},
        }

        # Group consumption by season and month using daily totals
        for date_str, daily_consumption in daily_totals.items():
            date = self._local_midnight(date_str)
            season = self._get_season(date)
            seasonal_data[season].append(daily_consumption)

            # Track monthly averages
            month_name = date.strftime("%B")  # Full month name
            if month_name not in seasonal_data["monthly"]:
                seasonal_data["monthly"][month_name] = []
            seasonal_data["monthly"][month_name].append(daily_consumption)

        # Calculate seasonal averages
        for season in ["winter", "spring", "summer", "autumn"]:
            if seasonal_data[season]:
                seasonal_data[f"{season}_avg"] = round(
                    statistics.mean(seasonal_data[season]), 1
                )
                seasonal_data[f"{season}_min"] = round(min(seasonal_data[season]), 1)
                seasonal_data[f"{season}_max"] = round(max(seasonal_data[season]), 1)

        # Calculate monthly averages
        for month in seasonal_data["monthly"]:
            if seasonal_data["monthly"][month]:
                seasonal_data["monthly"][month] = round(
                    statistics.mean(seasonal_data["monthly"][month]), 1
                )

        # Get current season stats
        current_season = self._get_season(dt_util.now())
        if seasonal_data[current_season]:
            seasonal_data["current_season"] = {
                "name": current_season,
                "avg": round(statistics.mean(seasonal_data[current_season]), 1),
                "min": round(min(seasonal_data[current_season]), 1),
                "max": round(max(seasonal_data[current_season]), 1),
            }

        return seasonal_data

    def _spread_consumption_over_days(self, liters_used: float, now: datetime) -> None:
        """Apportion `liters_used` across the days it was actually burnt over.

        Consumption is only ever observed as a drop between two polls, so oil
        burnt while Home Assistant was down (or between sparse tank readings)
        belongs to the days it spanned rather than to the day we noticed it.
        """
        if not self._last_update:
            # No baseline to spread from - attribute it all to now.
            self._consumption_history_with_dates.append((now, liters_used))
            return

        interval_seconds = (now - self._last_update).total_seconds()
        if interval_seconds < 24 * 3600:
            # Same-day (or clock went backwards) - no spreading needed.
            self._consumption_history_with_dates.append((now, liters_used))
            return

        # Weight each calendar day by how much of the interval fell inside it,
        # so the shares sum back to exactly liters_used. The previous approach
        # divided by the fractional elapsed days while iterating whole dates
        # inclusive, which over-attributed: a 1.2-day gap spanning two dates
        # credited 2 x (liters / 1.2), i.e. 1.67x the oil actually burnt.
        day = self._last_update.date()
        last_day = now.date()
        while day <= last_day:
            day_start = datetime.combine(
                day, datetime.min.time(), tzinfo=self._last_update.tzinfo
            )
            day_end = day_start + timedelta(days=1)
            overlap = (
                min(now, day_end) - max(self._last_update, day_start)
            ).total_seconds()
            if overlap > 0:
                self._consumption_history_with_dates.append(
                    (day_start, liters_used * overlap / interval_seconds)
                )
            day += timedelta(days=1)

    def _refresh_rolling_average(self, now: datetime) -> Dict[str, float]:
        """Recompute the rolling daily rate over complete days only.

        Today's bucket is still filling, so averaging it alongside finished
        days drags the rate down - a day that is three hours old contributes
        three hours of oil but a full day of weight. Earlier days do fill in:
        the next detection interval starts where the last one ended and tops
        up that day's bucket, so the current day is the only one that is ever
        genuinely incomplete. (The very first day tracked is also partial, but
        it ages out of the window within a week.)

        Returns the regrouped daily totals so callers can reuse them.
        """
        daily_totals = self._calculate_daily_totals_from_history()

        today = now.date().isoformat()
        complete_days = [
            liters for date_str, liters in daily_totals.items() if date_str < today
        ]
        # On a fresh install today may be all there is; reporting a partial
        # day beats reporting nothing at all.
        window = complete_days or list(daily_totals.values())

        self._daily_consumption_history = window[-CONSUMPTION_ROLLING_DAYS:]
        if self._daily_consumption_history:
            self._daily_consumption_usable_liters = sum(
                self._daily_consumption_history
            ) / len(self._daily_consumption_history)
        else:
            self._daily_consumption_usable_liters = 0.0

        return daily_totals

    def _record_consumption(self, liters_used: float, now: datetime) -> None:
        """Record observed consumption and refresh the derived averages.

        Shared by the volume-derived and percentage-derived detection paths so
        the two can't drift apart.
        """
        self._total_consumption_usable_liters += liters_used
        self._total_consumption_usable_kwh += liters_used * self._kwh_per_litre

        self._spread_consumption_over_days(liters_used, now)
        self._refresh_rolling_average(now)

        current_season = self._calculate_seasonal_stats().get("current_season", {})
        _LOGGER.info(
            "Updated daily consumption to %s L/day (rolling %d-day average). "
            "Current %s average: %s L/day (min: %s, max: %s)",
            round(self._daily_consumption_usable_liters, 1),
            len(self._daily_consumption_history),
            current_season.get("name", "season"),
            current_season.get("avg", 0),
            current_season.get("min", 0),
            current_season.get("max", 0),
        )

        # Consumption was detected, so the next interval starts from here.
        self._last_update = now

    async def _save_consumption_data(self) -> None:
        """Save consumption data to storage.

        Saved under this entry's id so multiple accounts never collide. As a
        transitional step we also drop any legacy key that refers to the same
        tank, keeping storage tidy after migration.
        """
        # Prefer the config entry id (stable, unique per instance). Fall back
        # to the scraped tank id, then the configured tank id, then "default"
        # for coordinators created outside a config entry (the config flow's
        # validation path does not persist state anyway).
        storage_key = self._entry_id
        if not storage_key:
            storage_key = (self.data or {}).get("id") or self._tank_id or "default"

        stored_data = await self._store.async_load() or {}

        tank_data = {
            "total_consumption_liters": self._total_consumption_usable_liters,
            "total_consumption_kwh": self._total_consumption_usable_kwh,
            "daily_consumption_liters": self._daily_consumption_usable_liters,
            "reference_volume": self._previous_usable_volume,
            "reference_level": self._previous_total_level,
            "consumption_history": self._daily_consumption_history,
            "consumption_history_with_dates": [
                [dt.isoformat(), cons]
                for dt, cons in self._consumption_history_with_dates
            ],
        }

        if self._last_update:
            tank_data["last_update"] = self._last_update.isoformat()

        stored_data[storage_key] = tank_data

        # Clean up legacy tank-id keyed entries that are now owned by this
        # config entry. The shared "default" bucket is left alone because we
        # can't safely tell whether it still belongs to another entry that
        # hasn't yet migrated.
        if self._entry_id:
            scraped_tank_id = (self.data or {}).get("id")
            for legacy_key in {self._tank_id, scraped_tank_id}:
                if legacy_key and legacy_key != self._entry_id:
                    stored_data.pop(legacy_key, None)

        await self._store.async_save(stored_data)

    async def async_close(self) -> None:
        """Close the private aiohttp session (call on unload)."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    def reset_consumption(self) -> None:
        """Reset the consumption counter."""
        self._total_consumption_usable_liters = 0.0
        self._total_consumption_usable_kwh = 0.0
        self._daily_consumption_usable_liters = 0.0
        self._daily_consumption_history = []  # Clear history
        self._previous_usable_volume = None
        self._previous_total_level = None
        self._last_update = None
        self._consumption_history_with_dates = []  # Clear seasonal history

        # Save the reset to storage
        self.hass.async_create_task(self._save_consumption_data())

    def force_consumption_reference(self, data: dict) -> None:
        """Set the current levels as reference points without resetting stats.

        Only validated readings become references. A missing level used to be
        coerced to 0, which made the very next poll look like the tank had
        been filled from empty.
        """
        volume = _volume_litres(data.get("usable_volume_litres"))
        level = _percentage(data.get("total_level_percentage"))

        if volume is None and level is None:
            _LOGGER.warning(
                "Refusing to set a consumption reference from a reading with "
                "neither a volume nor a level"
            )
            return

        self._previous_usable_volume = volume
        self._previous_total_level = level
        self._last_update = dt_util.now()

        _LOGGER.info(
            "Force-set reference values: usable_volume=%s L, total_level=%s%%",
            volume,
            level,
        )

        # Save the new reference values
        self.hass.async_create_task(self._save_consumption_data())

    def _ensure_session(self) -> aiohttp.ClientSession:
        """Return this coordinator's private aiohttp session, creating it once."""
        if self._session is None:
            # Dedicated cookie jar so concurrent BoilerJuice accounts never
            # share login state with each other or with other HA integrations.
            self._session = async_create_clientsession(
                self.hass,
                cookie_jar=aiohttp.CookieJar(),
                timeout=REQUEST_TIMEOUT,
            )
        return self._session

    async def _async_get_text(self, url: str, description: str) -> str:
        """GET `url` and return its body, mapping failures onto UpdateFailed."""
        try:
            async with self._session.get(url) as response:
                if response.status != 200:
                    raise BoilerJuiceConnectionError(
                        f"Failed to load the {description} (HTTP {response.status})"
                    )
                return await response.text()
        except aiohttp.ClientError as err:
            raise BoilerJuiceConnectionError(
                f"Failed to load the {description}: {err}"
            ) from err
        except TimeoutError as err:
            raise BoilerJuiceConnectionError(
                f"Timed out loading the {description}"
            ) from err

    async def _async_login(self) -> None:
        """Drive the BoilerJuice sign-in flow for this session."""
        login_page = await self._async_get_text(LOGIN_URL, "BoilerJuice login page")

        soup = BeautifulSoup(login_page, "html.parser")
        csrf_token = soup.find("meta", {"name": "csrf-token"})
        if not csrf_token or not csrf_token.get("content"):
            raise BoilerJuiceConnectionError(
                "Could not find the CSRF token on the BoilerJuice login page"
            )

        login_data = {
            "user[email]": self._get_config_value(CONF_EMAIL),
            "user[password]": self._get_config_value(CONF_PASSWORD),
            "authenticity_token": csrf_token["content"],
            "commit": "Sign in",
        }

        try:
            async with self._session.post(LOGIN_URL, data=login_data) as response:
                if response.status != 200:
                    raise BoilerJuiceConnectionError(
                        f"Login request failed (HTTP {response.status})"
                    )
                text = await response.text()
                # A rejected login re-renders the sign-in form, so the reliable
                # signals are the final URL and the presence of the password
                # field - not the words "Sign in", which also appear in the
                # signed-in header.
                landed_on_login = str(response.url).rstrip("/") == LOGIN_URL.rstrip("/")
        except aiohttp.ClientError as err:
            raise BoilerJuiceConnectionError(f"Login request failed: {err}") from err
        except TimeoutError as err:
            raise BoilerJuiceConnectionError("Login request timed out") from err

        if landed_on_login and looks_like_login_page(text):
            raise BoilerJuiceAuthError("Invalid credentials")

    async def _async_get_tank_id(self) -> str | None:
        """Return the first tank id linked from the tanks listing page."""
        text = await self._async_get_text(TANKS_URL, "BoilerJuice tanks page")
        if looks_like_login_page(text):
            raise BoilerJuiceAuthError(
                "BoilerJuice redirected to the sign-in page while listing tanks"
            )

        tank_ids = parse_tank_ids(text)
        if not tank_ids:
            return None
        if len(tank_ids) > 1:
            _LOGGER.debug(
                "Found %d tanks on this account; using the first one. Set a "
                "tank id in the integration options to pick a different tank",
                len(tank_ids),
            )
        return tank_ids[0]

    def _calculate_days_until_empty(self, data: dict[str, Any]) -> float | None:
        """Calculate the estimated days until empty."""
        current_volume = data.get("current_volume_litres")
        if current_volume is None:
            return None

        # If we have actual consumption data, use it
        if (
            self._daily_consumption_usable_liters
            and self._daily_consumption_usable_liters > 0
        ):
            return round(current_volume / self._daily_consumption_usable_liters, 1)

        # Otherwise, estimate based on current level and capacity
        capacity = data.get("capacity_litres")
        level = data.get("total_level_percentage")

        if capacity and level is not None and level > 0:
            # Assume average daily consumption of 2% of tank capacity
            estimated_daily_consumption = capacity * 0.02
            return round(current_volume / estimated_daily_consumption, 1)

        return None

    def _detect_consumption(self, data: Dict[str, Any], now: datetime) -> None:
        """Update the running totals from a validated reading.

        Called only once the reading has passed validation, so no branch here
        can be reached with a placeholder zero.
        """
        volume = data.get("usable_volume_litres")
        level = data.get("total_level_percentage")
        capacity = data.get("capacity_litres")

        if self._previous_usable_volume is None and self._previous_total_level is None:
            _LOGGER.info(
                "First update or reference values missing - setting initial "
                "values without calculating consumption"
            )
            self.force_consumption_reference(data)
            return

        consumption_detected = False

        # Direct volume change is the more precise of the two signals.
        if volume is not None and self._previous_usable_volume is not None:
            if volume > self._previous_usable_volume:
                _LOGGER.info(
                    "Detected tank refill: +%s L (from %s L to %s L)",
                    round(volume - self._previous_usable_volume, 1),
                    self._previous_usable_volume,
                    volume,
                )
                # Reset last update time so next consumption starts from now
                self._last_update = now
            elif volume < self._previous_usable_volume:
                liters_used = self._previous_usable_volume - volume
                _LOGGER.info(
                    "Detected consumption from volume change: %s L (from %s L to %s L)",
                    round(liters_used, 1),
                    self._previous_usable_volume,
                    volume,
                )
                consumption_detected = True
                self._record_consumption(liters_used, now)

        # Fall back to the percentage, which needs a capacity to become litres.
        if (
            not consumption_detected
            and level is not None
            and self._previous_total_level is not None
            and capacity is not None
        ):
            if level > self._previous_total_level:
                liters_added = ((level - self._previous_total_level) / 100) * capacity
                _LOGGER.info(
                    "Detected tank refill from level change: +%s%% (+%s L) - "
                    "tank capacity: %s L",
                    round(level - self._previous_total_level, 1),
                    round(liters_added, 1),
                    capacity,
                )
                self._last_update = now
            elif level < self._previous_total_level:
                percent_change = self._previous_total_level - level
                liters_used = (percent_change / 100) * capacity
                _LOGGER.info(
                    "Detected consumption from level change: %s%% (%s L) - "
                    "tank capacity: %s L",
                    round(percent_change, 1),
                    round(liters_used, 1),
                    capacity,
                )
                self._record_consumption(liters_used, now)

        # Only advance a reference we actually have a fresh reading for. A
        # reading that carries a level but no volume must not blank the volume
        # reference, or the next poll would book the whole tank as consumed.
        if volume is not None:
            self._previous_usable_volume = volume
        if level is not None:
            self._previous_total_level = level

    def _publish_derived_values(self, data: Dict[str, Any], now: datetime) -> None:
        """Attach the derived consumption figures to a reading."""
        # Recalculate rolling average on every coordinator run (not just when
        # consumption was detected) so old incorrect data ages out after 7 days.
        if self._consumption_history_with_dates:
            daily_totals = self._refresh_rolling_average(now)

            # Collapse history to one entry per day and keep just over a year
            # of it. Seasonal averages need every season represented, so a
            # short window would leave three of the four empty; the daily
            # rollup is what every consumer reads anyway, so this loses
            # nothing and bounds the stored rows.
            cutoff_date = now - timedelta(days=SEASONAL_HISTORY_DAYS)
            self._consumption_history_with_dates = [
                (day_start, liters)
                for day_start, liters in (
                    (self._local_midnight(date_str), liters)
                    for date_str, liters in daily_totals.items()
                )
                if day_start >= cutoff_date
            ]

        # kWh is derived from litres with the configured energy content, so a
        # changed "kWh per litre" is reflected everywhere on the next poll
        # instead of leaving the total contradicting the cost sensors.
        self._total_consumption_usable_kwh = (
            self._total_consumption_usable_liters * self._kwh_per_litre
        )

        data["total_consumption_usable_liters"] = self._total_consumption_usable_liters
        data["total_consumption_usable_kwh"] = self._total_consumption_usable_kwh
        data["daily_consumption_usable_liters"] = self._daily_consumption_usable_liters
        data["days_until_empty"] = self._calculate_days_until_empty(data)
        data["seasonal_stats"] = self._calculate_seasonal_stats()
        data["kwh_per_litre"] = self._kwh_per_litre

    async def _async_add_oil_price(self, data: Dict[str, Any]) -> None:
        """Attach the current kerosene price, keeping the last good value.

        The price is a nice-to-have from a separate public page. A failure
        here must not blank the price sensors or fail the whole update.
        """
        try:
            async with self._session.get(PRICE_URL) as response:
                if response.status == 200:
                    price_match = _PRICE_RE.search(await response.text())
                    if price_match:
                        price = _finite(price_match.group(1))
                        if price is not None and 0 < price <= MAX_PRICE_PENCE:
                            self._last_price_pence = price
                            self._last_price_updated = dt_util.now()
                        else:
                            _LOGGER.debug("Ignoring implausible oil price %s", price)
                else:
                    _LOGGER.debug("Oil price page returned HTTP %s", response.status)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Could not refresh the oil price: %s", err)

        if self._last_price_pence is not None:
            data["current_price_pence"] = self._last_price_pence
            if self._last_price_updated is not None:
                data["price_last_updated"] = self._last_price_updated.isoformat()

    async def _async_update_data(self):
        """Fetch data from BoilerJuice."""
        if not self._consumption_data_loaded:
            await self._load_consumption_data()

        self._ensure_session()

        try:
            await self._async_login()

            tank_id = self._tank_id or await self._async_get_tank_id()
            if not tank_id:
                raise BoilerJuiceParseError(
                    "Could not find a tank on this BoilerJuice account"
                )

            tank_page = await self._async_get_text(
                f"{TANKS_URL}/{tank_id}/edit", "BoilerJuice tank page"
            )

            # Parse and validate before touching any stored state. Nothing
            # below this line can be reached by a page we could not read.
            data = parse_tank_page(tank_page, tank_id)

            now = dt_util.now()
            self._detect_consumption(data, now)
            self._publish_derived_values(data, now)
            await self._async_add_oil_price(data)

            _LOGGER.debug(
                "Consumption data: total=%s L, daily=%s L/day, total_kwh=%s",
                round(self._total_consumption_usable_liters, 1),
                round(self._daily_consumption_usable_liters, 1),
                round(self._total_consumption_usable_kwh, 1),
            )

            self.hass.async_create_task(self._save_consumption_data())
            return data

        except UpdateFailed:
            # Expected scrape/login failure - the coordinator logs it as a
            # single warning and retries on the next interval.
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error in _async_update_data: %s", err)
            raise UpdateFailed(f"Unexpected error updating tank data: {err}") from err
