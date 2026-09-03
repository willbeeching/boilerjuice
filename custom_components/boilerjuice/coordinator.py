"""Data update coordinator for BoilerJuice.

Orchestration only: fetch through the client, decide what the reading means
with the consumption engine, publish, persist. Parsing lives in parser.py,
HTTP in client.py, the maths in consumption.py and the durable state in
storage.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Union

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
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
from .storage import ConsumptionState, ConsumptionStore

_LOGGER = logging.getLogger(__name__)

# Update every hour to allow smooth accumulation of energy consumption.
SCAN_INTERVAL = timedelta(hours=1)

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

        self._state = ConsumptionState()
        self._sample_days = 0
        self._kwh_per_litre = self._validated_kwh_per_litre()

        # The oil price comes from a separate, optional request. Keep the last
        # good value so one failed fetch does not blank the price sensors.
        self._last_price_pence: float | None = None
        self._last_price_updated: datetime | None = None

        self._entry_id: str | None = (
            config.entry_id if isinstance(config, ConfigEntry) else None
        )
        self._tank_id = validate_tank_id(self._config_value(CONF_TANK_ID))
        if self._tank_id is None and self._config_value(CONF_TANK_ID):
            _LOGGER.warning(
                "Ignoring the configured BoilerJuice tank id because it is not "
                "numeric; the first tank on the account will be used instead"
            )

        # Coordinators built outside a config entry (the config flow's
        # validation path) never persist anything.
        self._store = (
            ConsumptionStore(hass, self._entry_id, self._tank_id)
            if self._entry_id
            else None
        )

        # Every mutation of the running totals - a poll, a reset, a manual
        # set - happens under this, and the write happens before the lock is
        # released. Without it a service call landing mid-poll could be
        # overwritten by the poll's own save.
        self._lock = asyncio.Lock()
        self._loaded = False

        self._client = BoilerJuiceClient(
            self._create_session,
            self._config_value(CONF_EMAIL, required=True),
            self._config_value(CONF_PASSWORD, required=True),
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Published state
    # ------------------------------------------------------------------

    @property
    def kwh_per_litre(self) -> float:
        """Return the configured energy content of a litre of oil."""
        return self._kwh_per_litre

    @property
    def total_consumption_usable_liters(self) -> float:
        """Return the total oil consumption in liters."""
        return self._state.total_litres

    @property
    def total_consumption_usable_kwh(self) -> float:
        """Return the total oil consumption in kWh.

        Always derived from litres, never stored independently, so it cannot
        drift from the litre total or from the configured energy content.
        """
        return self._state.total_litres * self._kwh_per_litre

    @property
    def daily_consumption_usable_liters(self) -> float | None:
        """Return the daily rate, or None when nothing has been measured.

        None rather than 0.0: "we have not seen a full day yet" and "this
        tank burns no oil" are different answers, and only one of them should
        drive a days-until-empty estimate.
        """
        return self._state.effective_daily_litres

    @property
    def consumption_sample_days(self) -> int:
        """Return how many complete days the daily rate averages."""
        return 0 if self._state.daily_override is not None else self._sample_days

    @property
    def daily_consumption_is_manual(self) -> bool:
        """Whether the published daily rate is a manual override."""
        return self._state.daily_override is not None

    @property
    def last_level_change(self) -> datetime | None:
        """Return when the tank level was last seen to change."""
        return self._state.last_update

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @staticmethod
    def _local_midnight(date_str: str) -> datetime:
        """Return local midnight for an ISO date key from the daily totals."""
        return datetime.fromisoformat(date_str).replace(
            tzinfo=dt_util.DEFAULT_TIME_ZONE
        )

    async def _async_load(self) -> None:
        """Load the stored state once, reporting anything unusable."""
        if self._loaded:
            return
        self._loaded = True

        if self._store is None:
            return

        state, problem = await self._store.async_load()
        self._state = state
        self._sample_days = len(
            consumption.rolling_window(
                consumption.daily_totals(state.history), dt_util.now()
            )
        )

        if problem is None:
            ir.async_delete_issue(self.hass, DOMAIN, self._storage_issue_id)
            return

        # The history is gone and consumption restarts from zero, so tell the
        # user rather than letting their statistics quietly reset.
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._storage_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="invalid_stored_data",
            translation_placeholders={"reason": problem},
        )

    @property
    def _storage_issue_id(self) -> str:
        """Return the repair issue id for this entry's storage."""
        return f"invalid_stored_data_{self._entry_id}"

    async def _async_persist(self) -> None:
        """Write the current state. Callers hold the lock."""
        if self._store is not None:
            await self._store.async_save(self._state)

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    async def async_close(self) -> None:
        """Close the client's session (call on unload)."""
        await self._client.async_close()

    async def async_remove_storage(self) -> None:
        """Delete this entry's stored history (call when the entry is removed)."""
        if self._store is not None:
            await self._store.async_remove()
        ir.async_delete_issue(self.hass, DOMAIN, self._storage_issue_id)

    async def async_reset_consumption(self) -> None:
        """Reset the counters, references and history, and persist that."""
        async with self._lock:
            self._state = self._state.cleared()
            self._sample_days = 0
            await self._async_persist()

    async def async_set_consumption(
        self, total_litres: float, daily_litres: float | None = None
    ) -> None:
        """Set the totals by hand and rebase the references on the last reading.

        `daily_litres` is a persistent override: it survives polls and
        restarts until `reset_consumption` clears it, rather than being
        recomputed away by the next update.
        """
        async with self._lock:
            self._state.total_litres = total_litres
            if daily_litres is not None:
                self._state.daily_override = daily_litres
            self._rebase_references(self.data or {})
            await self._async_persist()

            if self.data:
                self.async_set_updated_data(self._decorate(dict(self.data)))

        _LOGGER.info(
            "Manually set consumption: total=%s L (%s kWh), daily=%s",
            total_litres,
            round(total_litres * self._kwh_per_litre, 1),
            "unchanged" if daily_litres is None else f"{daily_litres} L/day",
        )

    async def async_force_consumption_reference(self, state: dict) -> None:
        """Rebase the references on `state` without resetting the totals."""
        async with self._lock:
            self._rebase_references(state)
            await self._async_persist()

    def _rebase_references(self, state: dict) -> None:
        """Point the references at a reading. Callers hold the lock.

        Only validated readings become references. A missing level used to be
        coerced to 0, which made the very next poll look like the tank had
        been filled from empty.
        """
        volume = volume_litres(state.get("usable_volume_litres"))
        level = percentage(state.get("total_level_percentage"))

        if volume is None and level is None:
            _LOGGER.warning(
                "Refusing to set a consumption reference from a reading with "
                "neither a volume nor a level"
            )
            return

        self._state.reference_volume = volume
        self._state.reference_level = level
        self._state.last_update = dt_util.now()

    # ------------------------------------------------------------------
    # Update cycle
    # ------------------------------------------------------------------

    def _record_consumption(self, litres: float, now: datetime) -> None:
        """Record observed consumption. Callers hold the lock."""
        self._state.total_litres += litres
        self._state.history.extend(
            consumption.allocate_over_days(litres, self._state.last_update, now)
        )
        self._refresh_rolling_average(now)

        # Consumption was detected, so the next interval starts from here.
        self._state.last_update = now

    def _refresh_rolling_average(self, now: datetime) -> dict[str, float]:
        """Recompute the rolling daily rate. Returns the regrouped totals."""
        totals = consumption.daily_totals(self._state.history)
        window = consumption.rolling_window(totals, now)
        self._sample_days = len(window)
        self._state.daily_litres = consumption.average(window) if window else None
        return totals

    def _apply_reading(self, reading: TankReading, now: datetime) -> None:
        """Move the running totals on from a validated reading."""
        if self._state.reference_volume is None and self._state.reference_level is None:
            _LOGGER.info(
                "First update or reference values missing - setting initial "
                "values without calculating consumption"
            )
            self._rebase_references(reading.as_state())
            return

        transition = consumption.classify(
            self._state.reference_volume, self._state.reference_level, reading
        )

        if transition.is_refill:
            _LOGGER.info(
                "Detected a tank refill of %s L from the %s",
                round(transition.litres_added, 1),
                transition.source,
            )
            # The next consumption interval starts from now.
            self._state.last_update = now
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
            self._state.reference_volume = reading.volume_litres
        if reading.level_percentage is not None:
            self._state.reference_level = reading.level_percentage

    def _decorate(self, state: dict[str, Any]) -> dict[str, Any]:
        """Attach the derived consumption figures to a published state."""
        daily = self.daily_consumption_usable_liters

        state["total_consumption_usable_liters"] = self._state.total_litres
        state["total_consumption_usable_kwh"] = self.total_consumption_usable_kwh
        state["daily_consumption_usable_liters"] = daily
        state["consumption_sample_days"] = self.consumption_sample_days
        state["daily_consumption_is_manual"] = self.daily_consumption_is_manual
        state["days_until_empty"] = consumption.days_until_empty(
            state.get("current_volume_litres"),
            state.get("capacity_litres"),
            state.get("total_level_percentage"),
            daily or 0.0,
        )
        state["kwh_per_litre"] = self._kwh_per_litre

        if self._last_price_pence is not None:
            state["current_price_pence"] = self._last_price_pence
            if self._last_price_updated is not None:
                state["price_last_updated"] = self._last_price_updated.isoformat()

        return state

    def _publish(self, reading: TankReading, now: datetime) -> dict[str, Any]:
        """Return the state dict for `reading`, with the derived figures."""
        state = reading.as_state()

        # Recalculate on every run, not just when consumption was detected,
        # so old incorrect data ages out after the rolling window.
        if self._state.history:
            totals = self._refresh_rolling_average(now)
            self._state.history = consumption.trim_history(
                totals, now, self._local_midnight
            )
            seasonal = consumption.seasonal_stats(totals, now, self._local_midnight)
        else:
            seasonal = {}

        state["seasonal_stats"] = seasonal
        return self._decorate(state)

    async def _async_refresh_price(self) -> None:
        """Refresh the oil price, keeping the last good value on failure."""
        price = await self._client.async_fetch_price()
        if price is not None:
            self._last_price_pence = price
            self._last_price_updated = dt_util.now()

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from BoilerJuice."""
        await self._async_load()

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

        await self._async_refresh_price()

        async with self._lock:
            now = dt_util.now()
            self._apply_reading(reading, now)
            state = self._publish(reading, now)
            await self._async_persist()

        _LOGGER.debug(
            "Consumption: total=%s L, daily=%s, sample days=%s",
            round(self._state.total_litres, 1),
            self._state.effective_daily_litres,
            self._sample_days,
        )
        return state
