"""Persisting one account's consumption history.

Each config entry gets its own storage document. The previous design had
every account read, mutate and rewrite one shared document, so two accounts
polling at the same time could each write a copy of the state they had read
before the other's change and silently drop it.

Stored data is validated on the way in. It is written by us, but it survives
crashes, hand edits and older versions of this integration, so it is treated
as untrusted: anything that fails validation is refused rather than allowed
to poison the running totals.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 2

# The single document every account used to share.
LEGACY_STORAGE_VERSION = 1
LEGACY_STORAGE_KEY = f"{DOMAIN}_consumption_data"

# Migrating out of the shared document is a read-modify-write across every
# account, so it happens one at a time.
LEGACY_MIGRATION_LOCK = f"{DOMAIN}_legacy_migration_lock"

# Bounds for values read back from disk.
MAX_TOTAL_LITRES = 10_000_000
MAX_VOLUME_LITRES = 100_000
MAX_HISTORY_ROWS = 1_000

DatedHistory = list[tuple[datetime, float]]


class InvalidStoredData(Exception):
    """The stored document could not be trusted."""


@dataclass(slots=True)
class ConsumptionState:
    """Everything about one tank that has to outlive a restart.

    Litres are the source of truth. kWh is always derived from them with the
    configured energy content, so changing "kWh per litre" cannot leave a
    stored total contradicting the cost sensors.
    """

    total_litres: float = 0.0
    # Energy is accumulated with the factor in force when each litre was
    # burnt, not recomputed from the litre total. The energy sensor is
    # TOTAL_INCREASING, so recalculating history after a change to "kWh per
    # litre" would show up in long-term statistics as a jump, or - if the
    # factor went down - as a meter reset. None means "not seeded yet", for
    # documents written before energy was stored.
    total_kwh: float | None = None
    # None means "no complete day has been measured yet", which the sensors
    # show as unknown rather than as a confident 0 L/day.
    daily_litres: float | None = None
    # A rate the user set by hand. It survives polls and restarts until it is
    # cleared, unlike the measured rate which is recomputed every update.
    daily_override: float | None = None
    reference_volume: int | None = None
    reference_level: float | None = None
    last_update: datetime | None = None
    history: DatedHistory = field(default_factory=list)

    @property
    def effective_daily_litres(self) -> float | None:
        """Return the rate to publish: the manual override, else measured."""
        return (
            self.daily_override
            if self.daily_override is not None
            else self.daily_litres
        )

    def cleared(self) -> ConsumptionState:
        """Return a fresh state, as `reset_consumption` produces."""
        return ConsumptionState()


def _number(value: Any, *, low: float, high: float) -> float:
    """Return a finite number inside the bounds, or raise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidStoredData(f"expected a number, found {type(value).__name__}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise InvalidStoredData("expected a finite number")
    if not low <= number <= high:
        raise InvalidStoredData(f"{number} is outside {low}..{high}")
    return number


def _optional_number(value: Any, *, low: float, high: float) -> float | None:
    """Return None, or a finite number inside the bounds."""
    return None if value is None else _number(value, low=low, high=high)


def _moment(value: Any) -> datetime:
    """Return a timezone-aware datetime, or raise."""
    if not isinstance(value, str):
        raise InvalidStoredData("expected an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise InvalidStoredData(f"unreadable timestamp: {value!r}") from err
    # Timestamps written before this integration became timezone-aware were
    # naive local wall-clock, so they are localized rather than reinterpreted.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return dt_util.as_local(parsed)


def _history(value: Any) -> DatedHistory:
    """Return validated, chronologically ordered, bounded history."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidStoredData("history must be a list")

    rows: DatedHistory = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise InvalidStoredData("each history row must be [timestamp, litres]")
        rows.append((_moment(row[0]), _number(row[1], low=0, high=MAX_TOTAL_LITRES)))

    ordered = sorted(rows, key=lambda row: row[0])
    if ordered != rows:
        _LOGGER.debug("Stored consumption history was out of order; sorting it")

    if len(ordered) > MAX_HISTORY_ROWS:
        _LOGGER.debug(
            "Stored consumption history had %d rows; keeping the newest %d",
            len(ordered),
            MAX_HISTORY_ROWS,
        )
        ordered = ordered[-MAX_HISTORY_ROWS:]

    return ordered


def state_from_document(document: Any) -> ConsumptionState:
    """Return the tank state a stored sub-document describes, or raise."""
    if not isinstance(document, dict):
        raise InvalidStoredData("the stored document is not an object")

    last_update = document.get("last_update")

    return ConsumptionState(
        total_litres=_number(
            document.get("total_litres", 0.0), low=0, high=MAX_TOTAL_LITRES
        ),
        total_kwh=_optional_number(
            document.get("total_kwh"), low=0, high=MAX_TOTAL_LITRES * 100
        ),
        daily_litres=_optional_number(
            document.get("daily_litres"), low=0, high=MAX_TOTAL_LITRES
        ),
        daily_override=_optional_number(
            document.get("daily_override"), low=0, high=MAX_TOTAL_LITRES
        ),
        reference_volume=(
            None
            if document.get("reference_volume") is None
            else int(
                _number(document["reference_volume"], low=0, high=MAX_VOLUME_LITRES)
            )
        ),
        reference_level=_optional_number(
            document.get("reference_level"), low=0, high=100
        ),
        last_update=None if last_update is None else _moment(last_update),
        history=_history(document.get("history")),
    )


def document_from_state(state: ConsumptionState) -> dict[str, Any]:
    """Return the document to persist for `state`."""
    return {
        "total_litres": state.total_litres,
        "total_kwh": state.total_kwh,
        "daily_litres": state.daily_litres,
        "daily_override": state.daily_override,
        "reference_volume": state.reference_volume,
        "reference_level": state.reference_level,
        "last_update": (
            None if state.last_update is None else state.last_update.isoformat()
        ),
        "history": [[moment.isoformat(), litres] for moment, litres in state.history],
    }


def state_from_legacy_document(document: Any) -> ConsumptionState:
    """Return the state a v1 (shared-document) slot describes, or raise."""
    if not isinstance(document, dict):
        raise InvalidStoredData("the stored document is not an object")

    last_update = document.get("last_update")
    daily = _optional_number(
        document.get("daily_consumption_liters"), low=0, high=MAX_TOTAL_LITRES
    )

    return ConsumptionState(
        total_litres=_number(
            document.get("total_consumption_liters", 0.0), low=0, high=MAX_TOTAL_LITRES
        ),
        # v1 stored energy alongside litres, so it carries straight across
        # and the sensor does not jump on upgrade.
        total_kwh=_optional_number(
            document.get("total_consumption_kwh"), low=0, high=MAX_TOTAL_LITRES * 100
        ),
        # v1 wrote 0.0 for "nothing measured yet", which is indistinguishable
        # from a genuine zero. Treat it as "not measured" so the sensor shows
        # unknown until a real day is recorded.
        daily_litres=daily or None,
        reference_volume=(
            None
            if document.get("reference_volume") is None
            else int(
                _number(document["reference_volume"], low=0, high=MAX_VOLUME_LITRES)
            )
        ),
        reference_level=_optional_number(
            document.get("reference_level"), low=0, high=100
        ),
        last_update=None if last_update is None else _moment(last_update),
        history=_history(document.get("consumption_history_with_dates")),
    )


@dataclass(slots=True)
class AccountState:
    """Everything one account's document holds."""

    tanks: dict[str, ConsumptionState] = field(default_factory=dict)
    # How many consecutive authoritative tank listings have not mentioned a
    # tank we know about. A failed listing never counts, so an outage cannot
    # remove anybody's devices.
    missing: dict[str, int] = field(default_factory=dict)
    # A v1 document we adopted but could not attribute to a tank, because v1
    # never recorded which tank it belonged to. It is claimed on the first
    # poll that finds exactly one tank, and dropped otherwise.
    unassigned: ConsumptionState | None = None


def account_from_document(document: Any) -> AccountState:
    """Return the account state a stored document describes, or raise."""
    if not isinstance(document, dict):
        raise InvalidStoredData("the stored document is not an object")

    tanks = document.get("tanks", {})
    if not isinstance(tanks, dict):
        raise InvalidStoredData("tanks must be an object")

    missing = document.get("missing", {})
    if not isinstance(missing, dict):
        raise InvalidStoredData("missing must be an object")

    unassigned = document.get("unassigned")

    return AccountState(
        tanks={tank_id: state_from_document(sub) for tank_id, sub in tanks.items()},
        missing={
            tank_id: int(_number(count, low=0, high=MAX_HISTORY_ROWS))
            for tank_id, count in missing.items()
        },
        unassigned=None if unassigned is None else state_from_document(unassigned),
    )


def document_from_account(account: AccountState) -> dict[str, Any]:
    """Return the document to persist for `account`."""
    return {
        "tanks": {
            tank_id: document_from_state(state)
            for tank_id, state in account.tanks.items()
        },
        "missing": dict(account.missing),
        "unassigned": (
            None
            if account.unassigned is None
            else document_from_state(account.unassigned)
        ),
    }


class ConsumptionStore:
    """One config entry's consumption document."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, tank_id: str | None = None
    ) -> None:
        """Set up the per-entry store and remember how to find legacy data."""
        self._hass = hass
        self._entry_id = entry_id
        self._tank_id = tank_id
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", private=True
        )
        self._legacy: Store[dict[str, Any]] = Store(
            hass, LEGACY_STORAGE_VERSION, LEGACY_STORAGE_KEY
        )

    @property
    def key(self) -> str:
        """Return this entry's storage key."""
        return f"{DOMAIN}.{self._entry_id}"

    async def async_load(self) -> tuple[AccountState, str | None]:
        """Return the stored state, plus a reason if it had to be discarded.

        A None reason means the state is either freshly loaded or a clean
        start with nothing to report.
        """
        document = await self._store.async_load()

        if document is not None:
            try:
                return account_from_document(document), None
            except InvalidStoredData as err:
                _LOGGER.warning(
                    "Discarding unusable stored BoilerJuice consumption data "
                    "for this account: %s",
                    err,
                )
                return AccountState(), str(err)

        migrated = await self._async_migrate_from_legacy()
        if migrated is not None:
            account, reason = migrated
            await self.async_save(account)
            return account, reason

        return AccountState(), None

    def _slot_in(self, shared: dict[str, Any]) -> str | None:
        """Return the key in the shared v1 document that belongs to us."""
        if self._entry_id in shared:
            return self._entry_id
        if self._tank_id and self._tank_id in shared:
            return self._tank_id
        if self._tank_id and shared.get("default"):
            # Only claim the shared "default" bucket when a tank id makes it
            # unambiguous. With several untagged accounts it could belong to
            # any of them, so it is left for whichever entry can prove
            # ownership.
            return "default"
        return None

    async def _async_migrate_from_legacy(
        self,
    ) -> tuple[AccountState, str | None] | None:
        """Adopt this entry's slot out of the shared v1 document."""
        lock = self._hass.data.setdefault(LEGACY_MIGRATION_LOCK, asyncio.Lock())

        async with lock:
            shared = await self._legacy.async_load()
            if not shared:
                return None

            slot = self._slot_in(shared)
            if slot is None:
                return None

            reason = None
            try:
                state = state_from_legacy_document(shared[slot])
            except InvalidStoredData as err:
                _LOGGER.warning(
                    "Discarding unusable legacy BoilerJuice consumption data: %s", err
                )
                state, reason = ConsumptionState(), str(err)

            # v1 recorded no tank id unless the slot was keyed by one, so an
            # entry-keyed or "default" slot has to wait for the first poll to
            # learn which tank it describes.
            known_tank = self._tank_id if slot != self._entry_id else None
            if slot.isdigit():
                known_tank = slot

            account = AccountState()
            if reason is not None:
                # Nothing worth carrying across; the entry starts fresh.
                pass
            elif known_tank:
                account.tanks[known_tank] = state
            else:
                account.unassigned = state

            _LOGGER.info(
                "Migrated BoilerJuice consumption history out of the shared "
                "storage document into this account's own"
            )

            del shared[slot]
            if shared:
                await self._legacy.async_save(shared)
            else:
                await self._legacy.async_remove()

            return account, reason

    async def async_save(self, account: AccountState) -> None:
        """Write `account`, replacing whatever was there."""
        await self._store.async_save(document_from_account(account))

    async def async_remove(self) -> None:
        """Delete this entry's document (called when the entry is removed)."""
        await self._store.async_remove()
