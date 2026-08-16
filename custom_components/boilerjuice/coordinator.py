"""Data update coordinator for BoilerJuice."""

from __future__ import annotations

import json
import logging
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
    ACCOUNT_URL,
    CONF_EMAIL,
    CONF_KWH_PER_LITRE,
    CONF_PASSWORD,
    CONF_TANK_ID,
    DEFAULT_KWH_PER_LITRE,
    DOMAIN,
    LOGIN_URL,
    TANKS_URL,
)

_LOGGER = logging.getLogger(__name__)

# Update every hour to allow smooth accumulation of energy consumption
SCAN_INTERVAL = timedelta(hours=1)

# Conversion factors
# 1 liter of heating oil = 10.35 kWh (typical value for heating oil)
LITERS_TO_KWH = 10.35

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


class BoilerJuiceAuthError(UpdateFailed):
    """BoilerJuice rejected the configured credentials."""


class BoilerJuiceConnectionError(UpdateFailed):
    """BoilerJuice could not be reached or the login flow could not be driven."""


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
        self._kwh_per_litre = self._get_config_value_optional(
            CONF_KWH_PER_LITRE, DEFAULT_KWH_PER_LITRE
        )
        # Add list to store daily consumption history
        self._daily_consumption_history = []
        # Add seasonal tracking
        self._consumption_history_with_dates: List[Tuple[datetime, float]] = []

        # Set up storage. Keyed per config entry so multiple accounts don't
        # collide on the legacy "default" bucket when no tank id is provided.
        self._entry_id: str | None = (
            config.entry_id if isinstance(config, ConfigEntry) else None
        )
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._tank_id = self._get_config_value_optional(CONF_TANK_ID)

        # Flag to track if data has been loaded
        self._consumption_data_loaded = False

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

        if stored_data:
            _LOGGER.debug("Loading stored consumption data: %s", stored_data)

        loaded = False

        if self._entry_id and self._entry_id in stored_data:
            self._apply_stored_data(
                f"entry {self._entry_id}", stored_data[self._entry_id]
            )
            loaded = True
        elif self._tank_id and self._tank_id in stored_data:
            # Legacy per-tank key – migrate into the entry-keyed slot.
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
                "No stored consumption data for entry %s / tank %s; starting fresh",
                self._entry_id,
                self._tank_id,
            )

        self._consumption_data_loaded = True
        _LOGGER.debug("Consumption data loading completed")

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
        daily_totals = {}

        for dt, consumption in self._consumption_history_with_dates:
            date_key = dt.date().isoformat()
            if date_key in daily_totals:
                daily_totals[date_key] += consumption
            else:
                daily_totals[date_key] = consumption

        # Sort by date
        sorted_daily_totals = dict(sorted(daily_totals.items()))

        return sorted_daily_totals

    def _calculate_seasonal_stats(self) -> Dict[str, Any]:
        """Calculate seasonal consumption statistics."""
        if not self._consumption_history_with_dates:
            return {}

        # Get daily totals first to avoid double-counting same-day updates
        daily_totals = self._calculate_daily_totals_from_history()

        if not daily_totals:
            return {}

        # Initialize seasonal data
        seasonal_data = {
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
                avg = statistics.mean(seasonal_data[season])
                min_val = min(seasonal_data[season])
                max_val = max(seasonal_data[season])
                seasonal_data[f"{season}_avg"] = round(avg, 1)
                seasonal_data[f"{season}_min"] = round(min_val, 1)
                seasonal_data[f"{season}_max"] = round(max_val, 1)

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
        self._total_consumption_usable_kwh += liters_used * LITERS_TO_KWH

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
        _LOGGER.debug("Saved consumption data under %s: %s", storage_key, tank_data)

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
        """Set the current levels as reference points without resetting consumption stats."""
        current_usable_volume = float(data.get("usable_volume_litres", 0))
        current_total_level = float(data.get("total_level_percentage", 0))

        self._previous_usable_volume = current_usable_volume
        self._previous_total_level = current_total_level
        self._last_update = dt_util.now()

        _LOGGER.info(
            "Force-set reference values: usable_volume=%s L, total_level=%s%%",
            current_usable_volume,
            current_total_level,
        )

        # Save the new reference values
        self.hass.async_create_task(self._save_consumption_data())

    async def _get_tank_id(self) -> str | None:
        """Get the tank ID from the tanks page."""
        _LOGGER.debug("Accessing tanks page to find tank ID...")
        async with self._session.get(TANKS_URL) as response:
            if response.status != 200:
                _LOGGER.error(
                    "Failed to access tanks page with status %s", response.status
                )
                return None

            text = await response.text()
            soup = BeautifulSoup(text, "html.parser")
            tank_links = soup.find_all("a", href=re.compile(r"/uk/users/tanks/\d+"))

            if not tank_links:
                _LOGGER.error("Could not find any tank links on the tanks page")
                return None

            tank_id = re.search(r"/uk/users/tanks/(\d+)", tank_links[0]["href"]).group(
                1
            )
            _LOGGER.debug("Found tank ID: %s", tank_id)
            return tank_id

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

    async def _async_update_data(self):
        """Fetch data from BoilerJuice."""
        # Ensure consumption data is loaded before first update
        if not self._consumption_data_loaded:
            _LOGGER.debug("Loading consumption data before first update")
            await self._load_consumption_data()

        if self._session is None:
            # Dedicated cookie jar so concurrent BoilerJuice accounts never
            # share login state with each other or with other HA integrations.
            self._session = async_create_clientsession(
                self.hass, cookie_jar=aiohttp.CookieJar()
            )

        try:
            # First, get the login page to get the CSRF token
            _LOGGER.debug("Getting login page for CSRF token...")
            async with self._session.get(LOGIN_URL) as response:
                if response.status != 200:
                    _LOGGER.error(
                        "Failed to get login page with status %s", response.status
                    )
                    raise BoilerJuiceConnectionError("Failed to get login page")

                text = await response.text()
                soup = BeautifulSoup(text, "html.parser")
                csrf_token = soup.find("meta", {"name": "csrf-token"})
                if not csrf_token:
                    _LOGGER.error("Could not find CSRF token")
                    raise BoilerJuiceConnectionError("Failed to get CSRF token")

                csrf_token = csrf_token["content"]

            # Login to the site
            login_data = {
                "user[email]": self._get_config_value(CONF_EMAIL),
                "user[password]": self._get_config_value(CONF_PASSWORD),
                "authenticity_token": csrf_token,
                "commit": "Sign in",
            }

            _LOGGER.debug("Logging in...")
            async with self._session.post(LOGIN_URL, data=login_data) as response:
                if response.status != 200:
                    _LOGGER.error("Login failed with status %s", response.status)
                    raise BoilerJuiceConnectionError("Failed to login to BoilerJuice")

                # Check if we're still on the login page (indicating failed login)
                text = await response.text()
                if "Sign in" in text:
                    _LOGGER.error("Login failed - still on login page")
                    raise BoilerJuiceAuthError("Invalid credentials")

            # Get or find tank ID
            tank_id = self._get_config_value_optional(CONF_TANK_ID)
            if not tank_id:
                tank_id = await self._get_tank_id()
                if not tank_id:
                    raise UpdateFailed("Could not find tank ID")

            # Get the tank details page
            tank_url = f"{TANKS_URL}/{tank_id}/edit"
            _LOGGER.debug("Accessing tank page at %s", tank_url)
            async with self._session.get(tank_url) as response:
                if response.status != 200:
                    _LOGGER.error(
                        "Failed to get tank page with status %s", response.status
                    )
                    raise UpdateFailed("Failed to get tank data from BoilerJuice")

                text = await response.text()
                soup = BeautifulSoup(text, "html.parser")
                data = {}

                # Get tank level percentage
                # NOTE: BoilerJuice now only provides a single oil level
                usable_level_div = soup.find("div", {"id": "usable-oil"})
                if usable_level_div:
                    oil_level = usable_level_div.find("div", {"class": "oil-level"})
                    if oil_level and oil_level.get("data-percentage"):
                        level_percent = float(oil_level["data-percentage"])
                        # Use the same level for both total and usable
                        data["total_level_percentage"] = level_percent
                        data["usable_level_percentage"] = level_percent
                        _LOGGER.debug("Found oil level: %s%%", level_percent)

                # Get tank size
                # NOTE: BoilerJuice changed from 'tank-size-count' to 'tank_size'
                tank_size_input = soup.find("input", {"id": "tank_size"})
                if tank_size_input and tank_size_input.get("value"):
                    data["capacity_litres"] = int(tank_size_input["value"])
                    _LOGGER.debug(
                        "Found tank capacity: %s litres", data["capacity_litres"]
                    )
                else:
                    _LOGGER.debug(
                        "Tank size input not found with new ID, trying old format"
                    )
                    # Fallback to old format
                    tank_size_input = soup.find("input", {"id": "tank-size-count"})
                    if tank_size_input and tank_size_input.get("value"):
                        data["capacity_litres"] = int(tank_size_input["value"])
                        _LOGGER.debug(
                            "Found tank capacity (old format): %s litres",
                            data["capacity_litres"],
                        )

                # Get tank height
                # NOTE: BoilerJuice changed from 'tank-height-count' to 'internal_height'
                tank_height_input = soup.find("input", {"id": "internal_height"})
                if tank_height_input and tank_height_input.get("value"):
                    data["height_cm"] = int(tank_height_input["value"])
                    _LOGGER.debug("Found tank height: %s cm", data["height_cm"])
                else:
                    _LOGGER.debug(
                        "Tank height input not found with new ID, trying old format"
                    )
                    # Fallback to old format
                    tank_height_input = soup.find("input", {"id": "tank-height-count"})
                    if tank_height_input and tank_height_input.get("value"):
                        data["height_cm"] = int(tank_height_input["value"])
                        _LOGGER.debug(
                            "Found tank height (old format): %s cm", data["height_cm"]
                        )

                # Look for volume information in text
                volume_texts = soup.find_all(
                    string=lambda text: text
                    and any(
                        word in text.lower()
                        for word in ["litre", "volume", "oil level"]
                    )
                )
                for text in volume_texts:
                    text = text.strip()

                    # Extract oil volume
                    # NOTE: BoilerJuice now only shows one volume (not separate usable/total)
                    if "litres of oil" in text.lower() or "litres oil" in text.lower():
                        match = re.search(
                            r"(\d+)\s*litres?\s+(?:of\s+)?oil", text.lower()
                        )
                        if match:
                            volume = int(match.group(1))
                            # Use the same volume for both current and usable
                            data["current_volume_litres"] = volume
                            data["usable_volume_litres"] = volume
                            _LOGGER.debug("Found oil volume: %s litres", volume)

                # Get tank name
                tank_name_input = soup.find(
                    "input", {"id": "tank_user_tanks_attributes_0_name"}
                )
                if tank_name_input and tank_name_input.get("value"):
                    data["name"] = tank_name_input["value"]
                    _LOGGER.debug("Found tank name: %s", data["name"])

                # Get tank manufacturer/model
                tank_model_input = soup.find("input", {"id": "tankModelInput"})
                if tank_model_input and tank_model_input.get("value"):
                    model_id = tank_model_input.get("value")
                    data["model_id"] = model_id
                    _LOGGER.debug("Found tank model ID: %s", model_id)

                    # Try to find the manufacturer data in the JavaScript
                    scripts = soup.find_all("script")
                    for script in scripts:
                        if script.string and "var jsonData = " in script.string:
                            _LOGGER.debug("Found jsonData variable")
                            script_text = script.string
                            start_idx = script_text.find("var jsonData = ")
                            if start_idx >= 0:
                                # Try to find where the JSON array ends
                                array_start = script_text.find("[", start_idx)
                                if array_start >= 0:
                                    bracket_count = 1
                                    array_end = array_start + 1
                                    while (
                                        array_end < len(script_text)
                                        and bracket_count > 0
                                    ):
                                        if script_text[array_end] == "[":
                                            bracket_count += 1
                                        elif script_text[array_end] == "]":
                                            bracket_count -= 1
                                        array_end += 1

                                    if bracket_count == 0:
                                        json_str = script_text[array_start:array_end]
                                        try:
                                            json_data = json.loads(json_str)
                                            # Find the manufacturer for our model ID
                                            for item in json_data:
                                                if str(item.get("id")) == str(model_id):
                                                    data["model"] = item.get(
                                                        "tank", {}
                                                    ).get("Description")
                                                    data["manufacturer"] = item.get(
                                                        "tank", {}
                                                    ).get("Brand")
                                                    _LOGGER.debug(
                                                        "Found manufacturer from JSON: %s",
                                                        data["model"],
                                                    )
                                                    break
                                        except json.JSONDecodeError as e:
                                            _LOGGER.error(
                                                "Failed to parse tank model JSON: %s", e
                                            )
                            break
                else:
                    _LOGGER.debug("Could not find tank model ID")

                # Get tank shape
                for shape in ["cuboid", "horizontal_cylinder", "vertical_cylinder"]:
                    shape_input = soup.find(
                        "input", {"type": "radio", "name": "tank-shape", "value": shape}
                    )
                    if shape_input and shape_input.get("checked"):
                        data["shape"] = shape.replace("_", " ").title()
                        _LOGGER.debug("Found tank shape: %s", data["shape"])
                        break

                # Get oil type
                oil_type_select = soup.find("select", {"id": "tank_oil_type_id"})
                if oil_type_select:
                    selected_option = oil_type_select.find("option", selected=True)
                    if selected_option:
                        data["oil_type"] = selected_option.text
                        _LOGGER.debug("Found oil type: %s", data["oil_type"])

                # Add tank ID
                data["id"] = tank_id

                if not data:
                    raise UpdateFailed("Could not find any tank details")

                # Calculate consumption based on usable oil
                current_usable_volume = float(data.get("usable_volume_litres", 0))
                current_total_level = float(data.get("total_level_percentage", 0))
                now = dt_util.now()

                # Log current state
                _LOGGER.debug(
                    "Current state: usable_volume=%s L, total_level=%s%%, previous_volume=%s L, previous_level=%s%%",
                    current_usable_volume,
                    current_total_level,
                    self._previous_usable_volume,
                    self._previous_total_level,
                )

                # If we don't have previous values, set them without calculating consumption
                if (
                    self._previous_usable_volume is None
                    or self._previous_total_level is None
                ):
                    _LOGGER.info(
                        "First update or reference values missing - setting initial values without calculating consumption"
                    )
                    self.force_consumption_reference(data)

                    # For manual consumption based on current value
                    # If both usable oil volumes and percentages are valid and seem to indicate consumption, calculate it
                    if data.get("capacity_litres") and current_total_level < 100:
                        capacity = data.get("capacity_litres")
                        # Calculate how much oil has been used (100% - current_level)%
                        estimated_used = ((100 - current_total_level) / 100) * capacity
                        _LOGGER.info(
                            "Estimated consumption based on current level (%s%%): %s L out of %s L capacity",
                            current_total_level,
                            round(estimated_used, 1),
                            capacity,
                        )
                else:
                    # Track consumption based on direct volume change if available
                    consumption_detected = False

                    # Check for refill first (volume went up)
                    if (
                        self._previous_usable_volume is not None
                        and current_usable_volume > self._previous_usable_volume
                    ):
                        liters_added = (
                            current_usable_volume - self._previous_usable_volume
                        )
                        _LOGGER.info(
                            "Detected tank refill: +%s L (from %s L to %s L)",
                            round(liters_added, 1),
                            self._previous_usable_volume,
                            current_usable_volume,
                        )
                        # Reset last update time so next consumption starts from now
                        self._last_update = now

                    # Check for consumption (volume went down)
                    elif (
                        self._previous_usable_volume is not None
                        and current_usable_volume < self._previous_usable_volume
                    ):
                        liters_used = (
                            self._previous_usable_volume - current_usable_volume
                        )
                        _LOGGER.info(
                            "Detected consumption from volume change: %s L (from %s L to %s L)",
                            round(liters_used, 1),
                            self._previous_usable_volume,
                            current_usable_volume,
                        )

                        consumption_detected = True
                        self._record_consumption(liters_used, now)

                    # If no consumption detected from volume, check percentage change
                    if (
                        not consumption_detected
                        and self._previous_total_level is not None
                    ):
                        _LOGGER.debug(
                            "Checking level change: current=%s%%, previous=%s%%",
                            current_total_level,
                            self._previous_total_level,
                        )

                        # Check for refill first (level went up)
                        if current_total_level > self._previous_total_level:
                            capacity = data.get("capacity_litres")
                            if capacity:
                                percent_change = (
                                    current_total_level - self._previous_total_level
                                )
                                liters_added = (percent_change / 100) * capacity
                                _LOGGER.info(
                                    "Detected tank refill from level change: +%s%% (+%s L) - tank capacity: %s L",
                                    round(percent_change, 1),
                                    round(liters_added, 1),
                                    capacity,
                                )
                                # Reset last update time so next consumption starts from now
                                self._last_update = now

                        # Check for consumption (level went down)
                        elif current_total_level < self._previous_total_level:
                            # Calculate liters based on percentage change
                            capacity = data.get("capacity_litres")
                            if capacity:
                                percent_change = (
                                    self._previous_total_level - current_total_level
                                )
                                liters_used = (percent_change / 100) * capacity
                                _LOGGER.info(
                                    "Detected consumption from level change: %s%% (%s L) - tank capacity: %s L",
                                    percent_change,
                                    liters_used,
                                    capacity,
                                )

                                consumption_detected = True
                                self._record_consumption(liters_used, now)

                    # Update previous values regardless of consumption
                    self._previous_usable_volume = current_usable_volume
                    self._previous_total_level = current_total_level
                    # Only update _last_update if consumption was detected (moved above)

                # Add consumption data to tank data
                data["total_consumption_usable_liters"] = (
                    self._total_consumption_usable_liters
                )
                data["total_consumption_usable_kwh"] = (
                    self._total_consumption_usable_kwh
                )
                data["daily_consumption_usable_liters"] = (
                    self._daily_consumption_usable_liters
                )
                data["days_until_empty"] = self._calculate_days_until_empty(data)

                # Recalculate rolling average on every coordinator run (not just when consumption detected)
                # This allows old incorrect data to naturally age out after 7 days
                if self._consumption_history_with_dates:
                    daily_totals = self._refresh_rolling_average(now)

                    _LOGGER.debug(
                        "Recalculated rolling average: %s L/day from %d complete days",
                        round(self._daily_consumption_usable_liters, 1),
                        len(self._daily_consumption_history),
                    )

                    # Collapse history to one entry per day and keep just over a
                    # year of it. Seasonal averages need every season represented,
                    # so a short window would leave three of the four empty; the
                    # daily rollup is what every consumer reads anyway, so this
                    # loses nothing and bounds the stored rows.
                    cutoff_date = now - timedelta(days=SEASONAL_HISTORY_DAYS)
                    self._consumption_history_with_dates = [
                        (day_start, liters)
                        for day_start, liters in (
                            (self._local_midnight(date_str), liters)
                            for date_str, liters in daily_totals.items()
                        )
                        if day_start >= cutoff_date
                    ]

                # Seasonal stats are recalculated on every refresh, not only when
                # consumption is detected. `data` is rebuilt from the scrape each
                # run, so anything set only inside a consumption branch is absent
                # (and the sensor unknown) on every other update.
                data["seasonal_stats"] = self._calculate_seasonal_stats()

                _LOGGER.info(
                    "Consumption data: total=%s L, daily=%s L/day, total_kwh=%s",
                    round(self._total_consumption_usable_liters, 1),
                    round(self._daily_consumption_usable_liters, 1),
                    round(self._total_consumption_usable_kwh, 1),
                )

                # Ensure correct kWh calculation (sometimes this might be out of sync)
                if (
                    abs(
                        self._total_consumption_usable_kwh
                        - (self._total_consumption_usable_liters * LITERS_TO_KWH)
                    )
                    > 0.1
                ):
                    _LOGGER.info(
                        "Correcting kWh value from %s to %s",
                        self._total_consumption_usable_kwh,
                        self._total_consumption_usable_liters * LITERS_TO_KWH,
                    )
                    self._total_consumption_usable_kwh = (
                        self._total_consumption_usable_liters * LITERS_TO_KWH
                    )
                    data["total_consumption_usable_kwh"] = (
                        self._total_consumption_usable_kwh
                    )

                # Get current oil price from kerosene prices page
                try:
                    async with self._session.get(
                        "https://www.boilerjuice.com/kerosene-prices/"
                    ) as price_response:
                        if price_response.status == 200:
                            price_text = await price_response.text()
                            price_match = re.search(
                                r"(\d+\.\d+)\s*pence per litre", price_text
                            )
                            if price_match:
                                data["current_price_pence"] = float(
                                    price_match.group(1)
                                )
                                _LOGGER.debug(
                                    "Found current oil price: %s pence per litre",
                                    data["current_price_pence"],
                                )
                except Exception as e:
                    _LOGGER.error("Error getting oil price: %s", e)

                # Add kWh per litre to the data
                data["kwh_per_litre"] = self._kwh_per_litre

                # Save consumption data to storage
                self.hass.async_create_task(self._save_consumption_data())

                return data

        except UpdateFailed:
            # Expected scrape/login failure - the coordinator logs it as a
            # single warning and retries on the next interval.
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error in _async_update_data: %s", err)
            raise UpdateFailed(f"Unexpected error updating tank data: {err}") from err
