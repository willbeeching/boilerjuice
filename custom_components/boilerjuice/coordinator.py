"""Data update coordinator for one BoilerJuice account.

Orchestration only. One config entry is one account; every tank on it is
tracked separately and published under its own key. Parsing lives in
parser.py, HTTP in client.py, the maths in consumption.py, per-tank
bookkeeping in tank.py and the durable state in storage.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
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
from .helpers import async_tank_device
from .models import TankReading
from .parser import finite, validate_tank_id
from .storage import (
    AccountState,
    ConsumptionState,
    ConsumptionStore,
    StorageWriteFailed,
)
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

# Failures are counted per tank. Anything that goes wrong before we get as
# far as a specific tank - listing the account, signing in - is counted
# under this key instead.
ACCOUNT_SCOPE = "__account__"

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
        # Health is per tank: one broken tank must not make the others look
        # broken, and a healthy tank must not clear a broken one's repair.
        self._parse_failures: dict[str, int] = {}
        self._failing: set[str] = set()
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

    def tanks_without_readings(
        self, tank_ids: Iterable[str] | None = None
    ) -> list[str]:
        """Return the targeted tanks that have nothing to rebase onto.

        `tank_ids` of None means the whole account, which is how an
        entry-wide action arrives. Setting the consumption on a tank with no
        current reading writes the new total but leaves the reference where
        it was, so the tank books the gap as consumption the moment it comes
        back.
        """
        published = self.data or {}
        return [
            tracker.tank_id
            for tracker in self._selected_trackers(tank_ids)
            if published.get(tracker.tank_id) is None
        ]

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
            # Tracking it again un-retires it, and it resumes the history it
            # was retired with.
            self._account.retired.discard(tank_id)
            state = self._account.tanks.get(tank_id)
            if state is None:
                state = ConsumptionState()
                self._account.tanks[tank_id] = state
            tracker = TankTracker(tank_id, state, midnight=self._local_midnight)
            tracker.refresh_sample_count(dt_util.now())
            self._trackers[tank_id] = tracker
        return tracker

    async def _async_load(self) -> None:
        """Load the stored state once, reporting anything unusable.

        The loaded flag is only set after a successful read. Setting it up
        front meant a single transient storage failure was remembered as "we
        have loaded", so the next refresh started from empty history and the
        first save overwrote perfectly good stored data.
        """
        if self._loaded:
            return

        if self._store is None:
            self._loaded = True
            return

        try:
            account, problem = await self._store.async_load()
        except Exception as err:
            # Nothing is marked loaded and nothing is persisted, so the next
            # refresh tries again against the untouched document.
            raise UpdateFailed(
                f"Could not read the stored consumption history: {err}"
            ) from err

        self._account = account
        for tank_id in list(account.tanks):
            if tank_id not in account.retired:
                self._tracker_for(tank_id)
        self._loaded = True

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
        """Write the current state. Callers hold the lock.

        Never writes before a successful load: doing so would replace stored
        history with whatever this process happens to hold.
        """
        if self._store is not None and self._loaded:
            await self._store.async_save(self._account)

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    async def async_close(self) -> None:
        """Close the client's session (call on unload)."""
        await self._client.async_close()

    async def async_reset_consumption(
        self, tank_ids: Iterable[str] | None = None
    ) -> None:
        """Reset the named tanks, or every tank on the account.

        Every named tank is reset, written and published in one pass under
        one lock. Calling this once per tank meant a failure on the second
        left the first permanently reset.
        """
        async with self._lock:
            trackers = self._selected_trackers(tank_ids)
            undo = [(tracker, tracker.snapshot()) for tracker in trackers]
            for tracker in trackers:
                tracker.reset()
                self._account.tanks[tracker.tank_id] = tracker.state
            try:
                await self._async_persist()
            except Exception:
                self._rollback(undo)
                raise

            # Published from what we now hold, rather than left to the next
            # poll. A reset is a local fact about stored history, and asking
            # BoilerJuice to confirm it meant the entities kept the old
            # total, or went unavailable, whenever the site was down.
            published = dict(self.data or {})
            for tracker in trackers:
                state = published.get(tracker.tank_id)
                if state is None:
                    continue
                updated = tracker.decorate(dict(state), self._kwh_per_litre)
                updated["last_level_change"] = tracker.last_level_change
                published[tracker.tank_id] = updated
            if published:
                self.async_set_updated_data(published)

    async def async_set_consumption(
        self,
        total_litres: float,
        daily_litres: float | None = None,
        tank_ids: Iterable[str] | None = None,
    ) -> None:
        """Set the totals by hand and rebase the references on the last reading.

        `daily_litres` is a persistent override: it survives polls and
        restarts until `reset_consumption` clears it, rather than being
        recomputed away by the next update.
        """
        async with self._lock:
            published = dict(self.data or {})
            trackers = self._selected_trackers(tank_ids)
            missing = [
                tracker.tank_id
                for tracker in trackers
                if published.get(tracker.tank_id) is None
            ]
            if missing:
                # Checked again here, under the lock, so a reading that
                # disappeared between the caller's check and this one cannot
                # leave half the account rebased and half of it not.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="tanks_without_readings",
                    translation_placeholders={"tanks": ", ".join(sorted(missing))},
                )
            # Snapshotted before anything moves. A failed write used to
            # leave the new totals in memory, reported as "nothing was
            # recorded", and then persisted them on the next poll anyway.
            undo = [(tracker, tracker.snapshot()) for tracker in trackers]
            for tracker in trackers:
                tracker.set_consumption(total_litres, daily_litres, self._kwh_per_litre)
                state = published[tracker.tank_id]
                tracker.rebase(state)
                tracker.state.last_update = dt_util.now()
                updated = tracker.decorate(dict(state), self._kwh_per_litre)
                updated["last_level_change"] = tracker.last_level_change
                published[tracker.tank_id] = updated
                self._account.tanks[tracker.tank_id] = tracker.state
            try:
                await self._async_persist()
            except Exception:
                self._rollback(undo)
                raise
            # Published only after the write succeeded, so what the entities
            # show is what survives a restart.
            if published:
                self.async_set_updated_data(published)

        _LOGGER.info(
            "Manually set consumption: total=%s L (%s kWh), daily=%s",
            total_litres,
            round(total_litres * self._kwh_per_litre, 1),
            "unchanged" if daily_litres is None else f"{daily_litres} L/day",
        )

    def _rollback(self, undo: list[tuple[TankTracker, tuple[Any, int]]]) -> None:
        """Put the trackers back as they were before a failed write.

        The account's own map is repointed too: `reset` replaces the state
        object rather than mutating it, so the map would otherwise still
        hold the state the failed write produced.
        """
        for tracker, snapshot in undo:
            tracker.restore(snapshot)
            self._account.tanks[tracker.tank_id] = tracker.state

    def _selected_trackers(self, tank_ids: Iterable[str] | None) -> list[TankTracker]:
        """Return the trackers an operation applies to.

        None means the whole account. Otherwise the account's own order is
        kept, and a name we do not track is skipped rather than invented.
        """
        if tank_ids is None:
            return list(self._trackers.values())
        wanted = set(tank_ids)
        return [
            tracker for tank_id, tracker in self._trackers.items() if tank_id in wanted
        ]

    # ------------------------------------------------------------------
    # Update cycle
    # ------------------------------------------------------------------

    def _selected(self) -> set[str] | None:
        """Return the tanks the user explicitly chose, or None for "all".

        This is the configuration, not the account: a tank outside it has
        been excluded on purpose, whatever BoilerJuice happens to list.
        """
        if self._pinned_tank_id:
            return {self._pinned_tank_id}
        included = self._config_value(CONF_TANKS)
        return set(included) if included else None

    def _wanted(self, listed: list[str]) -> list[str]:
        """Return the tanks this entry should track, in listing order."""
        if self._pinned_tank_id:
            return [self._pinned_tank_id]

        selected = self._selected()
        if selected is not None:
            return [tank_id for tank_id in listed if tank_id in selected]
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
        but a failure across the board is a real failure. Each tank's health
        is recorded separately so a broken one goes unavailable on its own
        rather than sitting there showing a stale reading.
        """
        readings: dict[str, TankReading] = {}
        failures: list[Exception] = []

        for tank_id in tank_ids:
            try:
                readings[tank_id] = await self._client.async_fetch_tank(tank_id)
            except BoilerJuiceAuthError:
                raise
            except UpdateFailed as err:
                self._note_failure(tank_id, err)
                failures.append(err)
            else:
                self._note_success(tank_id)

        if not readings and failures:
            raise failures[0]
        return readings

    def _reconcile(self, wanted: list[str]) -> list[str]:
        """Decide which tracked tanks to stop tracking.

        Two different things make a tank stop belonging here, and they differ
        only in how quickly we act:

        - The user excluded it, or pinned a different one. That is a
          deliberate choice made just now, so act on it immediately. Whether
          BoilerJuice still lists it is irrelevant, which is why this is
          decided from the configuration rather than from the listing.
        - BoilerJuice stopped listing a tank the user did select. That could
          be an account change or a bad page, so wait for three consecutive
          authoritative listings to agree.

        Either way the tank is retired, not erased: its device goes but its
        history stays, and it picks that history back up if it returns. A
        scraped page saying a tank is absent is not good enough evidence to
        delete somebody's consumption record.

        Filtering used to be applied only when fetching, so an excluded tank
        kept its tracker, its device and its entities for ever.
        """
        removals: list[str] = []
        wanted_set = set(wanted)
        selected = self._selected()

        for tank_id in list(self._trackers):
            if tank_id in wanted_set:
                self._account.missing.pop(tank_id, None)
                continue

            if selected is not None and tank_id not in selected:
                _LOGGER.info(
                    "A tank is no longer included in this account's "
                    "configuration; removing its device but keeping its history"
                )
                removals.append(tank_id)
                continue

            seen_missing = self._account.missing.get(tank_id, 0) + 1
            self._account.missing[tank_id] = seen_missing
            if seen_missing >= MISSING_LISTINGS_BEFORE_REMOVAL:
                _LOGGER.info(
                    "A tank has been absent from %d consecutive BoilerJuice "
                    "listings; removing its device but keeping its history",
                    MISSING_LISTINGS_BEFORE_REMOVAL,
                )
                removals.append(tank_id)

        return removals

    def _forget(self, tank_id: str) -> None:
        """Retire a tank: drop its device and tracker, keep its history."""
        self._trackers.pop(tank_id, None)
        self._account.missing.pop(tank_id, None)
        self._forget_health(tank_id)
        self._account.retired.add(tank_id)

        if not self._entry_id:
            return
        device = async_tank_device(self.hass, tank_id, self._entry_id)
        if device is not None:
            # Not async_update_device(remove_config_entry_id=...): Home
            # Assistant 2026.9 reports that as deprecated and due to stop
            # working in 2027.8, because a device now belongs to exactly one
            # config entry. async_remove_device exists as far back as the
            # supported floor, so this needs no bump.
            dr.async_get(self.hass).async_remove_device(device.id)

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

    def _note_failure(self, scope: str, err: Exception) -> None:
        """Record that `scope` could not be read, and log the transition.

        The first failure is a warning; the ones after it are debug. An
        hourly poll against a tank that has been broken for a week would
        otherwise write 168 identical warnings into the log.
        """
        if scope not in self._failing:
            self._failing.add(scope)
            _LOGGER.warning("A BoilerJuice tank could not be read: %s", err)
        else:
            _LOGGER.debug("A BoilerJuice tank still cannot be read: %s", err)

        if isinstance(err, BoilerJuiceParseError):
            self._parse_failures[scope] = self._parse_failures.get(scope, 0) + 1
        self._refresh_layout_issue()

    def _note_success(self, scope: str) -> None:
        """Record that `scope` is readable again."""
        if scope in self._failing:
            self._failing.discard(scope)
            _LOGGER.info("A BoilerJuice tank is readable again")
        self._parse_failures.pop(scope, None)
        self._refresh_layout_issue()

    def _forget_health(self, scope: str) -> None:
        """Drop the health record for a tank we no longer track."""
        self._failing.discard(scope)
        self._parse_failures.pop(scope, None)
        self._refresh_layout_issue()

    def _refresh_layout_issue(self) -> None:
        """Raise or clear the "the site changed" repair.

        Driven by the worst tank, not by the last one polled: a healthy tank
        must not clear a repair raised for a permanently broken sibling.
        """
        stuck = any(
            count >= PARSE_FAILURES_BEFORE_REPAIR
            for count in self._parse_failures.values()
        )
        if stuck:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._layout_issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="page_layout_changed",
                learn_more_url="https://github.com/willbeeching/boilerjuice/issues",
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self._layout_issue_id)

    async def _async_collect(
        self,
    ) -> tuple[list[str], list[str], dict[str, TankReading]]:
        """List the account's tanks and read the ones we want.

        Maps every failure onto the right coordinator outcome: rejected
        credentials become a reauth flow rather than an endless hourly retry.
        Failures are attributed to the right scope, so a tank that will not
        parse is counted against that tank and not against the account.

        Returns (listed, wanted, readings). A listing that selects no tanks
        is still an authoritative listing and is returned rather than raised
        on, so the caller can reconcile against it before reporting failure.
        """
        try:
            listed = await self._async_list_tanks()
        except BoilerJuiceAuthError as err:
            self._client.invalidate_session()
            raise ConfigEntryAuthFailed(str(err)) from err
        except UpdateFailed as err:
            self._note_failure(ACCOUNT_SCOPE, err)
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error listing BoilerJuice tanks")
            raise UpdateFailed(f"Unexpected error listing tanks: {err}") from err

        self._note_success(ACCOUNT_SCOPE)

        wanted = self._wanted(listed)
        if not wanted:
            # Nothing to read, but the listing was authoritative: the caller
            # reconciles against it and then reports the failure. Raising
            # here left a selected tank that had vanished stuck for ever,
            # because its absence was never counted.
            return listed, [], {}

        try:
            # Individual failures are recorded per tank inside this call.
            return listed, wanted, await self._async_fetch_readings(wanted)
        except BoilerJuiceAuthError as err:
            self._client.invalidate_session()
            raise ConfigEntryAuthFailed(str(err)) from err
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

        listed, wanted, readings = await self._async_collect()

        await self._async_refresh_price()

        known_before = set(self._trackers)

        async with self._lock:
            now = dt_util.now()
            self._claim_unassigned(listed)

            self._last_successful_update = now

            published: dict[str, dict[str, Any]] = {}
            for tank_id, reading in readings.items():
                tracker = self._tracker_for(tank_id)
                tracker.apply(reading, now, self._kwh_per_litre)
                state = tracker.publish(reading, now, self._kwh_per_litre)
                state["last_level_change"] = tracker.last_level_change
                state["last_successful_update"] = now
                published[tank_id] = self._with_price(state)

            # A tank we could not read keeps its internal history but is not
            # republished, so its entities go unavailable rather than showing
            # a stale reading that looks current.
            for tank_id in self._reconcile(wanted):
                self._forget(tank_id)
                published.pop(tank_id, None)

            try:
                await self._async_persist()
            except StorageWriteFailed:
                # The readings are good; only the record of them failed. The
                # entities stay up, because taking them down would not put
                # the disk right, and the next successful write carries the
                # totals. Said out loud, because Home Assistant's own Store
                # only whispers it.
                _LOGGER.warning(
                    "Could not write this BoilerJuice account's consumption "
                    "history. The running totals are correct but will not "
                    "survive a restart until a later write succeeds"
                )

            if not wanted:
                # Reported only after reconciling, so the removal counting
                # above has already run and been persisted.
                raise UpdateFailed(
                    "No BoilerJuice tank on this account matches the "
                    "integration's configuration"
                )

            self._register_devices(published)

            # No `and known_before` guard: the first refresh happens before
            # the platforms register their listeners, so there is nobody to
            # notify then anyway. Requiring a non-empty `known_before` meant
            # an account whose every tank had been retired never got entities
            # for the next tank it gained - the device appeared, with nothing
            # on it.
            discovered = [
                tank_id for tank_id in published if tank_id not in known_before
            ]
            for listener in self._new_tank_listeners:
                listener(discovered)

            # Returned from inside the lock on purpose. Home Assistant
            # assigns this snapshot to `self.data` the moment we return, and
            # an action publishing its own snapshot has to take this lock
            # first, so the two cannot interleave. Nothing between building
            # `published` and returning it suspends today; keeping the return
            # inside the lock means nothing added here later can either.
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
