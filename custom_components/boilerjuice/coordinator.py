"""Data update coordinator for BoilerJuice.

Orchestration only: fetch through the client, decide what the reading means
with the consumption engine, publish, persist. Parsing lives in parser.py,
HTTP in client.py, and the maths in consumption.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Union

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import consumption
from .client import BoilerJuiceClient
from .const import (
    CONF_EMAIL,
    CONF_KWH_PER_LITRE,
    CONF_PASSWORD,
    CONF_TANK_ID,
    DEFAULT_KWH_PER_LITRE,
    DOMAIN,
)
from .models import TankReading
from .parser import finite, percentage, validate_tank_id, volume_litres

_LOGGER = logging.getLogger(__name__)

# Update every hour to allow smooth accumulation of energy consumption.
SCAN_INTERVAL = timedelta(hours=1)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_consumption_data"

# Re-exported so the rest of the integration has one import site for these.
CONSUMPTION_ROLLING_DAYS = consumption.CONSUMPTION_ROLLING_DAYS
SEASONAL_HISTORY_DAYS = consumption.SEASONAL_HISTORY_DAYS


class BoilerJuiceDataUpdateCoordinator(DataUpdateCoordinator):
    """Fetch BoilerJuice data and maintain the consumption history."""

    def __init__(
        self, hass: HomeAssistant, config: Union[ConfigEntry, dict[str, Any]]
    ) -> None:
        """Initialize coordinator."""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self._config = config

        self._previous_usable_volume: float | None = None
        self._previous_total_level: float | None = None
        self._total_consumption_usable_liters = 0.0
        self._total_consumption_usable_kwh = 0.0
        self._daily_consumption_usable_liters = 0.0
        self._last_update: datetime | None = None
        self._kwh_per_litre = self._validated_kwh_per_litre()
        self._daily_consumption_history: list[float] = []
        self._consumption_history_with_dates: consumption.DatedHistory = []

        # The oil price comes from a separate, optional request. Keep the last
        # good value so one failed fetch does not blank the price sensors.
        self._last_price_pence: float | None = None
        self._last_price_updated: datetime | None = None

        self._entry_id: str | None = (
            config.entry_id if isinstance(config, ConfigEntry) else None
        )
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._tank_id = validate_tank_id(self._config_value(CONF_TANK_ID))
        if self._tank_id is None and self._config_value(CONF_TANK_ID):
            _LOGGER.warning(
                "Ignoring the configured BoilerJuice tank id because it is not "
                "numeric; the first tank on the account will be used instead"
            )

        self._client = BoilerJuiceClient(
            self._create_session,
            self._config_value(CONF_EMAIL, required=True),
            self._config_value(CONF_PASSWORD, required=True),
        )
        self._consumption_data_loaded = False

    def _create_session(self, timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession:
        """Create a session owned by Home Assistant, with a private cookie jar.

        Sharing HA's default session made two BoilerJuice accounts overwrite
        each other's login cookies (GitHub issue #3).
        """
        return async_create_clientsession(
            self.hass, cookie_jar=aiohttp.CookieJar(), timeout=timeout
        )

    def _config_value(self, key: str, *, required: bool = False, default: Any = None):
        """Read a value from either a ConfigEntry or a plain dict."""
        data = (
            self._config.data if isinstance(self._config, ConfigEntry) else self._config
        )
        return data[key] if required else data.get(key, default)

    def _validated_kwh_per_litre(self) -> float:
        """Return the configured energy content, falling back to the default."""
        configured = finite(
            self._config_value(CONF_KWH_PER_LITRE, default=DEFAULT_KWH_PER_LITRE)
        )
        if configured is None or configured <= 0:
            _LOGGER.warning(
                "Configured kWh per litre is not a positive number; using %s",
                DEFAULT_KWH_PER_LITRE,
            )
            return DEFAULT_KWH_PER_LITRE
        return configured

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

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

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

        self._consumption_history_with_dates = [
            (self._as_local(datetime.fromisoformat(moment)), litres)
            for moment, litres in data.get("consumption_history_with_dates", [])
        ]

        last_update = data.get("last_update")
        if last_update:
            try:
                self._last_update = self._as_local(datetime.fromisoformat(last_update))
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

        stored = await self._store.async_load() or {}

        if self._entry_id and self._entry_id in stored:
            self._apply_stored_data(f"entry {self._entry_id}", stored[self._entry_id])
        elif self._tank_id and self._tank_id in stored:
            # Legacy per-tank key - migrated into the entry-keyed slot on save.
            self._apply_stored_data(
                "a legacy tank-keyed document", stored[self._tank_id]
            )
        elif self._tank_id and stored.get("default"):
            # Only migrate the legacy "default" bucket when we can be sure it
            # belongs to this entry (i.e. a tank id is explicitly configured).
            # With multiple untagged accounts the default bucket is ambiguous,
            # so we leave it untouched rather than risk cross-contamination.
            self._apply_stored_data("the legacy default document", stored["default"])

        self._consumption_data_loaded = True

    async def _save_consumption_data(self) -> None:
        """Save consumption data to storage under this entry's key."""
        storage_key = self._entry_id
        if not storage_key:
            # Coordinators created outside a config entry (the config flow's
            # validation path) do not persist anything meaningful.
            storage_key = (self.data or {}).get("id") or self._tank_id or "default"

        stored = await self._store.async_load() or {}

        document = {
            "total_consumption_liters": self._total_consumption_usable_liters,
            "total_consumption_kwh": self._total_consumption_usable_kwh,
            "daily_consumption_liters": self._daily_consumption_usable_liters,
            "reference_volume": self._previous_usable_volume,
            "reference_level": self._previous_total_level,
            "consumption_history": self._daily_consumption_history,
            "consumption_history_with_dates": [
                [moment.isoformat(), litres]
                for moment, litres in self._consumption_history_with_dates
            ],
        }
        if self._last_update:
            document["last_update"] = self._last_update.isoformat()

        stored[storage_key] = document

        # Clean up legacy tank-id keyed entries now owned by this config
        # entry. The shared "default" bucket is left alone because we can't
        # safely tell whether it still belongs to an entry that hasn't
        # migrated yet.
        if self._entry_id:
            scraped_tank_id = (self.data or {}).get("id")
            for legacy_key in {self._tank_id, scraped_tank_id}:
                if legacy_key and legacy_key != self._entry_id:
                    stored.pop(legacy_key, None)

        await self._store.async_save(stored)

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    async def async_close(self) -> None:
        """Close the client's session (call on unload)."""
        await self._client.async_close()

    def reset_consumption(self) -> None:
        """Reset the consumption counters and references."""
        self._total_consumption_usable_liters = 0.0
        self._total_consumption_usable_kwh = 0.0
        self._daily_consumption_usable_liters = 0.0
        self._daily_consumption_history = []
        self._previous_usable_volume = None
        self._previous_total_level = None
        self._last_update = None
        self._consumption_history_with_dates = []

        self.hass.async_create_task(self._save_consumption_data())

    def force_consumption_reference(self, data: dict) -> None:
        """Set the current levels as references without resetting the stats.

        Only validated readings become references. A missing level used to be
        coerced to 0, which made the very next poll look like the tank had
        been filled from empty.
        """
        volume = volume_litres(data.get("usable_volume_litres"))
        level = percentage(data.get("total_level_percentage"))

        if volume is None and level is None:
            _LOGGER.warning(
                "Refusing to set a consumption reference from a reading with "
                "neither a volume nor a level"
            )
            return

        self._previous_usable_volume = volume
        self._previous_total_level = level
        self._last_update = dt_util.now()

        self.hass.async_create_task(self._save_consumption_data())

    # ------------------------------------------------------------------
    # Update cycle
    # ------------------------------------------------------------------

    def _record_consumption(self, litres: float, now: datetime) -> None:
        """Record observed consumption and refresh the derived averages."""
        self._total_consumption_usable_liters += litres
        self._total_consumption_usable_kwh += litres * self._kwh_per_litre

        self._consumption_history_with_dates.extend(
            consumption.allocate_over_days(litres, self._last_update, now)
        )
        self._refresh_rolling_average(now)

        # Consumption was detected, so the next interval starts from here.
        self._last_update = now

    def _refresh_rolling_average(self, now: datetime) -> dict[str, float]:
        """Recompute the rolling daily rate. Returns the regrouped totals."""
        totals = consumption.daily_totals(self._consumption_history_with_dates)
        self._daily_consumption_history = consumption.rolling_window(totals, now)
        self._daily_consumption_usable_liters = consumption.average(
            self._daily_consumption_history
        )
        return totals

    def _apply_reading(self, reading: TankReading, now: datetime) -> None:
        """Move the running totals on from a validated reading."""
        if self._previous_usable_volume is None and self._previous_total_level is None:
            _LOGGER.info(
                "First update or reference values missing - setting initial "
                "values without calculating consumption"
            )
            self.force_consumption_reference(reading.as_state())
            return

        transition = consumption.classify(
            self._previous_usable_volume, self._previous_total_level, reading
        )

        if transition.is_refill:
            _LOGGER.info(
                "Detected a tank refill of %s L from the %s",
                round(transition.litres_added, 1),
                transition.source,
            )
            # The next consumption interval starts from now.
            self._last_update = now
        elif transition.is_consumption:
            _LOGGER.info(
                "Detected consumption of %s L from the %s",
                round(transition.litres_consumed, 1),
                transition.source,
            )
            self._record_consumption(transition.litres_consumed, now)

        # Only advance a reference we actually have a fresh reading for. A
        # reading that carries a level but no volume must not blank the volume
        # reference, or the next poll would book the whole tank as consumed.
        if reading.volume_litres is not None:
            self._previous_usable_volume = reading.volume_litres
        if reading.level_percentage is not None:
            self._previous_total_level = reading.level_percentage

    def _publish(self, reading: TankReading, now: datetime) -> dict[str, Any]:
        """Return the state dict for `reading`, with the derived figures."""
        state = reading.as_state()

        # Recalculate on every run, not just when consumption was detected,
        # so old incorrect data ages out after the rolling window.
        if self._consumption_history_with_dates:
            totals = self._refresh_rolling_average(now)
            self._consumption_history_with_dates = consumption.trim_history(
                totals, now, self._local_midnight
            )
            seasonal = consumption.seasonal_stats(totals, now, self._local_midnight)
        else:
            seasonal = {}

        # kWh is derived from litres with the configured energy content, so a
        # changed "kWh per litre" is reflected everywhere on the next poll
        # instead of leaving the total contradicting the cost sensors.
        self._total_consumption_usable_kwh = (
            self._total_consumption_usable_liters * self._kwh_per_litre
        )

        state["total_consumption_usable_liters"] = self._total_consumption_usable_liters
        state["total_consumption_usable_kwh"] = self._total_consumption_usable_kwh
        state["daily_consumption_usable_liters"] = self._daily_consumption_usable_liters
        state["days_until_empty"] = consumption.days_until_empty(
            reading.volume_litres,
            reading.capacity_litres,
            reading.level_percentage,
            self._daily_consumption_usable_liters,
        )
        state["seasonal_stats"] = seasonal
        state["kwh_per_litre"] = self._kwh_per_litre

        if self._last_price_pence is not None:
            state["current_price_pence"] = self._last_price_pence
            if self._last_price_updated is not None:
                state["price_last_updated"] = self._last_price_updated.isoformat()

        return state

    async def _async_refresh_price(self) -> None:
        """Refresh the oil price, keeping the last good value on failure."""
        price = await self._client.async_fetch_price()
        if price is not None:
            self._last_price_pence = price
            self._last_price_updated = dt_util.now()

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from BoilerJuice."""
        if not self._consumption_data_loaded:
            await self._load_consumption_data()

        try:
            reading = await self._client.async_fetch_tank(self._tank_id)
        except UpdateFailed:
            # A failure means we have no reading, so nothing is applied and
            # the previous state stands. The coordinator logs one warning and
            # retries on the next interval.
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error updating BoilerJuice data")
            raise UpdateFailed(f"Unexpected error updating tank data: {err}") from err

        now = dt_util.now()
        self._apply_reading(reading, now)
        await self._async_refresh_price()
        state = self._publish(reading, now)

        _LOGGER.debug(
            "Consumption data: total=%s L, daily=%s L/day, total_kwh=%s",
            round(self._total_consumption_usable_liters, 1),
            round(self._daily_consumption_usable_liters, 1),
            round(self._total_consumption_usable_kwh, 1),
        )

        self.hass.async_create_task(self._save_consumption_data())
        return state
