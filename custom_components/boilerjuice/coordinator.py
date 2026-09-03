"""Data update coordinator for one BoilerJuice account.

Orchestration only. One config entry is one account; every tank on it is
tracked separately and published under its own key. Parsing lives in
parser.py, HTTP in client.py, the maths in consumption.py, per-tank
bookkeeping in tank.py and the durable state in storage.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import consumption
from .client import BoilerJuiceClient
from .const import (
    CONF_EMAIL,
    CONF_KWH_PER_LITRE,
    CONF_PASSWORD,
    CONF_TANK_ID,
    CONF_TANKS,
    DEFAULT_KWH_PER_LITRE,
    DOMAIN,
)
from .errors import BoilerJuiceAuthError, BoilerJuiceParseError
from .models import TankReading
from .parser import finite, validate_tank_id
from .storage import AccountState, ConsumptionState, ConsumptionStore
from .tank import TankTracker

_LOGGER = logging.getLogger(__name__)

# Update every hour to allow smooth accumulation of energy consumption.
SCAN_INTERVAL = timedelta(hours=1)

# How many consecutive authoritative listings must omit a tank before its
# device is removed. A listing that failed is not authoritative and never
# counts, so an outage cannot delete anybody's devices.
MISSING_LISTINGS_BEFORE_REMOVAL = 3

# BoilerJuice is scraped, so the page shape can change under us. One bad
# poll is noise; this many in a row is a site change the user needs to
# know about, because readings have stopped and only an update will fix it.
PARSE_FAILURES_BEFORE_REPAIR = 3

# Re-exported so the rest of the integration has one import site for these.
CONSUMPTION_ROLLING_DAYS = consumption.CONSUMPTION_ROLLING_DAYS
SEASONAL_HISTORY_DAYS = consumption.SEASONAL_HISTORY_DAYS


class BoilerJuiceDataUpdateCoordinator(
    DataUpdateCoordinator[dict[str, dict[str, Any]]]
):
    """Fetch one account's tanks and maintain their consumption history."""

    def __init__(
        self, hass: HomeAssistant, config: ConfigEntry | dict[str, Any]
    ) -> None:
        """Initialize coordinator."""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self._config = config

        self._account = AccountState()
        self._trackers: dict[str, TankTracker] = {}
        self._kwh_per_litre = self._validated_kwh_per_litre()

        # The oil price is account-wide and comes from a separate, optional
        # request. Keep the last good value so one failed fetch does not blank
        # the price sensors.
        self._last_price_pence: float | None = None
        self._last_price_updated: datetime | None = None

        self._entry_id: str | None = (
            config.entry_id if isinstance(config, ConfigEntry) else None
        )
        self._pinned_tank_id = validate_tank_id(self._config_value(CONF_TANK_ID))
        if self._pinned_tank_id is None and self._config_value(CONF_TANK_ID):
            _LOGGER.warning(
                "Ignoring the configured BoilerJuice tank id because it is not "
                "numeric; every tank on the account will be tracked instead"
            )

        # Coordinators built outside a config entry (the config flow's
        # validation path) never persist anything.
        self._store = (
            ConsumptionStore(hass, self._entry_id, self._pinned_tank_id)
            if self._entry_id
            else None
        )

        # Every mutation of the running totals - a poll, a reset, a manual
        # set - happens under this, and the write happens before the lock is
        # released. Without it a service call landing mid-poll could be
        # overwritten by the poll's own save.
        self._lock = asyncio.Lock()
        self._loaded = False
        self._consecutive_parse_failures = 0
        self._last_successful_update: datetime | None = None
        self._new_tank_listeners: list[Callable[[list[str]], None]] = []

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

    def _config_value(
        self, key: str, *, required: bool = False, default: Any = None
    ) -> Any:
        """Read a value from the entry's options, then its data."""
        data: Mapping[str, Any]
        if isinstance(self._config, ConfigEntry):
            if key in self._config.options:
                return self._config.options[key]
            data = self._config.data
        else:
            data = self._config
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
    def tank_ids(self) -> list[str]:
        """Return the tanks this account currently publishes."""
        return list(self._trackers)

    def device_info(self, tank_id: str) -> DeviceInfo:
        """Return the device this tank's entities belong to.

        A real oil tank, not a service entry: it is a physical thing in a
        specific place, so it takes an area and shows up as equipment.
        """
        state = (self.data or {}).get(tank_id, {})
        return DeviceInfo(
            identifiers={(DOMAIN, tank_id)},
            name=state.get("name") or state.get("model") or "BoilerJuice Tank",
            manufacturer=state.get("manufacturer", "BoilerJuice"),
            model=state.get("model"),
            configuration_url="https://www.boilerjuice.com/uk",
        )

    def tracker(self, tank_id: str) -> TankTracker | None:
        """Return the tracker for `tank_id`, if it is still known."""
        return self._trackers.get(tank_id)

    def reading(self, tank_id: str) -> dict[str, Any] | None:
        """Return the published state for `tank_id`, if there is one."""
        return (self.data or {}).get(tank_id)

    @callback
    def async_add_new_tank_listener(
        self, listener: Callable[[list[str]], None]
    ) -> None:
        """Register a callback for tanks discovered after setup."""
        self._new_tank_listeners.append(listener)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @staticmethod
    def _local_midnight(date_str: str) -> datetime:
        """Return local midnight for an ISO date key from the daily totals."""
        return datetime.fromisoformat(date_str).replace(
            tzinfo=dt_util.DEFAULT_TIME_ZONE
        )

    def _tracker_for(self, tank_id: str) -> TankTracker:
        """Return (creating if needed) the tracker for `tank_id`."""
        tracker = self._trackers.get(tank_id)
        if tracker is None:
            state = self._account.tanks.get(tank_id)
            if state is None:
                state = ConsumptionState()
                self._account.tanks[tank_id] = state
            tracker = TankTracker(tank_id, state, midnight=self._local_midnight)
            tracker.refresh_sample_count(dt_util.now())
            self._trackers[tank_id] = tracker
        return tracker

    async def _async_load(self) -> None:
        """Load the stored state once, reporting anything unusable."""
        if self._loaded:
            return
        self._loaded = True

        if self._store is None:
            return

        account, problem = await self._store.async_load()
        self._account = account
        for tank_id in list(account.tanks):
            self._tracker_for(tank_id)

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
            await self._store.async_save(self._account)

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    async def async_close(self) -> None:
        """Close the client's session (call on unload)."""
        await self._client.async_close()

    async def async_reset_consumption(self, tank_id: str | None = None) -> None:
        """Reset one tank, or every tank on the account."""
        async with self._lock:
            for tracker in self._selected_trackers(tank_id):
                tracker.reset()
                self._account.tanks[tracker.tank_id] = tracker.state
            await self._async_persist()

    async def async_set_consumption(
        self,
        total_litres: float,
        daily_litres: float | None = None,
        tank_id: str | None = None,
    ) -> None:
        """Set the totals by hand and rebase the references on the last reading.

        `daily_litres` is a persistent override: it survives polls and
        restarts until `reset_consumption` clears it, rather than being
        recomputed away by the next update.
        """
        async with self._lock:
            published = dict(self.data or {})
            for tracker in self._selected_trackers(tank_id):
                tracker.set_consumption(total_litres, daily_litres)
                state = published.get(tracker.tank_id)
                if state is not None:
                    tracker.rebase(state)
                    tracker.state.last_update = dt_util.now()
                    updated = tracker.decorate(dict(state), self._kwh_per_litre)
                    updated["last_level_change"] = tracker.last_level_change
                    published[tracker.tank_id] = updated
            await self._async_persist()
            if published:
                self.async_set_updated_data(published)

        _LOGGER.info(
            "Manually set consumption: total=%s L (%s kWh), daily=%s",
            total_litres,
            round(total_litres * self._kwh_per_litre, 1),
            "unchanged" if daily_litres is None else f"{daily_litres} L/day",
        )

    def _selected_trackers(self, tank_id: str | None) -> list[TankTracker]:
        """Return the trackers an operation applies to."""
        if tank_id is None:
            return list(self._trackers.values())
        tracker = self._trackers.get(tank_id)
        return [tracker] if tracker else []

    # ------------------------------------------------------------------
    # Update cycle
    # ------------------------------------------------------------------

    def _wanted(self, listed: list[str]) -> list[str]:
        """Return the tanks this entry should track, in listing order."""
        if self._pinned_tank_id:
            return [self._pinned_tank_id]

        included = self._config_value(CONF_TANKS)
        if included:
            return [tank_id for tank_id in listed if tank_id in included]
        return listed

    async def _async_refresh_price(self) -> None:
        """Refresh the oil price, keeping the last good value on failure."""
        price = await self._client.async_fetch_price()
        if price is not None:
            self._last_price_pence = price
            self._last_price_updated = dt_util.now()

    def _with_price(self, state: dict[str, Any]) -> dict[str, Any]:
        """Attach the account-wide oil price to a tank's published state."""
        if self._last_price_pence is not None:
            state["current_price_pence"] = self._last_price_pence
            if self._last_price_updated is not None:
                state["price_last_updated"] = self._last_price_updated.isoformat()
        return state

    async def _async_list_tanks(self) -> list[str]:
        """Return the tanks on the account, or just the pinned one."""
        if self._pinned_tank_id:
            return [self._pinned_tank_id]
        return await self._client.async_list_tank_ids()

    async def _async_fetch_readings(
        self, tank_ids: list[str]
    ) -> dict[str, TankReading]:
        """Fetch each wanted tank, tolerating individual failures.

        One tank that will not parse must not cost the others their update,
        but a failure across the board is a real failure.
        """
        readings: dict[str, TankReading] = {}
        failures: list[Exception] = []

        for tank_id in tank_ids:
            try:
                readings[tank_id] = await self._client.async_fetch_tank(tank_id)
            except BoilerJuiceAuthError:
                raise
            except UpdateFailed as err:
                _LOGGER.warning("Could not read one of the tanks: %s", err)
                failures.append(err)

        if not readings and failures:
            raise failures[0]
        return readings

    def _note_absences(self, listed: list[str]) -> list[str]:
        """Count tanks missing from an authoritative listing; return removals."""
        removals = []
        for tank_id in list(self._trackers):
            if tank_id in listed:
                self._account.missing.pop(tank_id, None)
                continue
            seen_missing = self._account.missing.get(tank_id, 0) + 1
            self._account.missing[tank_id] = seen_missing
            if seen_missing >= MISSING_LISTINGS_BEFORE_REMOVAL:
                removals.append(tank_id)
        return removals

    def _forget(self, tank_id: str) -> None:
        """Drop a tank that BoilerJuice has consistently stopped listing."""
        _LOGGER.info(
            "A tank has been absent from %d consecutive BoilerJuice listings; "
            "removing it",
            MISSING_LISTINGS_BEFORE_REMOVAL,
        )
        self._trackers.pop(tank_id, None)
        self._account.tanks.pop(tank_id, None)
        self._account.missing.pop(tank_id, None)

        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, tank_id)})
        if device is not None and self._entry_id:
            registry.async_update_device(
                device.id, remove_config_entry_id=self._entry_id
            )

    def _claim_unassigned(self, tank_ids: list[str]) -> None:
        """Attach migrated v1 history to its tank, if we can tell which.

        The v1 document never recorded a tank id, so it can only be claimed
        when the account turns out to have exactly one tank.
        """
        if self._account.unassigned is None:
            return

        if len(tank_ids) == 1 and tank_ids[0] not in self._account.tanks:
            self._account.tanks[tank_ids[0]] = self._account.unassigned
            _LOGGER.info("Attached the migrated consumption history to this tank")
        else:
            _LOGGER.warning(
                "Could not tell which of this account's %d tanks the migrated "
                "consumption history belongs to; starting those tanks fresh",
                len(tank_ids),
            )
        self._account.unassigned = None

    @property
    def _layout_issue_id(self) -> str:
        """Return the repair issue id for a changed BoilerJuice page."""
        return f"page_layout_changed_{self._entry_id}"

    def _note_parse_failure(self) -> None:
        """Count a page we could not read, and raise a repair if it persists."""
        self._consecutive_parse_failures += 1
        if self._consecutive_parse_failures != PARSE_FAILURES_BEFORE_REPAIR:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._layout_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="page_layout_changed",
            learn_more_url="https://github.com/willbeeching/boilerjuice/issues",
        )

    def _clear_parse_failures(self) -> None:
        """Forget the failure run once a page parses again."""
        if self._consecutive_parse_failures:
            self._consecutive_parse_failures = 0
            ir.async_delete_issue(self.hass, DOMAIN, self._layout_issue_id)

    async def _async_collect(self) -> tuple[list[str], dict[str, TankReading]]:
        """List the account's tanks and read the ones we want.

        Maps every failure onto the right coordinator outcome: rejected
        credentials become a reauth flow rather than an endless hourly retry.
        """
        try:
            listed = await self._async_list_tanks()
            wanted = self._wanted(listed)
            if not wanted:
                raise UpdateFailed(
                    "No BoilerJuice tank on this account matches the "
                    "integration's configuration"
                )
            return listed, await self._async_fetch_readings(wanted)
        except BoilerJuiceAuthError as err:
            self._client.invalidate_session()
            raise ConfigEntryAuthFailed(str(err)) from err
        except BoilerJuiceParseError:
            self._note_parse_failure()
            raise
        except UpdateFailed:
            # No reading means nothing is applied and the previous state
            # stands. The coordinator logs one warning and retries.
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error updating BoilerJuice data")
            raise UpdateFailed(f"Unexpected error updating tank data: {err}") from err

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch every wanted tank on the account."""
        await self._async_load()

        listed, readings = await self._async_collect()
        self._clear_parse_failures()

        await self._async_refresh_price()

        known_before = set(self._trackers)

        async with self._lock:
            now = dt_util.now()
            self._claim_unassigned(listed)

            self._last_successful_update = now

            published: dict[str, dict[str, Any]] = {}
            for tank_id, reading in readings.items():
                tracker = self._tracker_for(tank_id)
                tracker.apply(reading, now)
                state = tracker.publish(reading, now, self._kwh_per_litre)
                state["last_level_change"] = tracker.last_level_change
                state["last_successful_update"] = now
                published[tank_id] = self._with_price(state)

            # Tanks we could not read this time keep their previous state
            # rather than disappearing from the dashboard.
            for tank_id, previous in (self.data or {}).items():
                published.setdefault(tank_id, previous)

            for tank_id in self._note_absences(listed):
                self._forget(tank_id)
                published.pop(tank_id, None)

            await self._async_persist()

        self._register_devices(published)

        discovered = [tank_id for tank_id in published if tank_id not in known_before]
        if discovered and known_before:
            for listener in self._new_tank_listeners:
                listener(discovered)

        return published

    @callback
    def _register_devices(self, published: dict[str, dict[str, Any]]) -> None:
        """Create or refresh one device per tank."""
        if not self._entry_id:
            return

        registry = dr.async_get(self.hass)
        for tank_id, state in published.items():
            registry.async_get_or_create(
                config_entry_id=self._entry_id,
                identifiers={(DOMAIN, tank_id)},
                name=state.get("name") or state.get("model") or "BoilerJuice Tank",
                manufacturer=state.get("manufacturer", "BoilerJuice"),
                model=state.get("model"),
                configuration_url="https://www.boilerjuice.com/uk",
            )

    async def async_remove_storage(self) -> None:
        """Delete this account's stored history and clear its repair issues."""
        if self._store is not None:
            await self._store.async_remove()
        ir.async_delete_issue(self.hass, DOMAIN, self._storage_issue_id)
        ir.async_delete_issue(self.hass, DOMAIN, self._layout_issue_id)
