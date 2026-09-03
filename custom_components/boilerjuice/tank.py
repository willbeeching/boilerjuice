"""Consumption bookkeeping for one tank.

An account can hold several tanks, and each keeps its own references, totals
and history. Everything here is synchronous and in-memory: the coordinator
owns the lock, the clock and the persistence.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import consumption
from .models import TankReading
from .parser import percentage, volume_litres
from .storage import ConsumptionState

_LOGGER = logging.getLogger(__name__)


class TankTracker:
    """The running consumption figures for one tank."""

    def __init__(
        self,
        tank_id: str,
        state: ConsumptionState | None = None,
        *,
        midnight,
    ) -> None:
        """Start tracking `tank_id` from `state`."""
        self.tank_id = tank_id
        self.state = state or ConsumptionState()
        self._midnight = midnight
        self._sample_days = 0

    # ------------------------------------------------------------------
    # Published figures
    # ------------------------------------------------------------------

    @property
    def total_litres(self) -> float:
        """Return the total oil consumption in litres."""
        return self.state.total_litres

    @property
    def daily_litres(self) -> float | None:
        """Return the daily rate, or None when nothing has been measured.

        None rather than 0.0: "we have not seen a full day yet" and "this
        tank burns no oil" are different answers, and only one of them should
        drive a days-until-empty estimate.
        """
        return self.state.effective_daily_litres

    @property
    def sample_days(self) -> int:
        """Return how many complete days the measured rate averages."""
        return 0 if self.state.daily_override is not None else self._sample_days

    @property
    def daily_is_manual(self) -> bool:
        """Whether the published daily rate is a manual override."""
        return self.state.daily_override is not None

    @property
    def last_level_change(self) -> datetime | None:
        """Return when this tank's level was last seen to change."""
        return self.state.last_update

    def total_kwh(self, kwh_per_litre: float) -> float:
        """Return the total in kWh.

        Always derived from litres, never stored independently, so it cannot
        drift from the litre total or from the configured energy content.
        """
        return self.state.total_litres * kwh_per_litre

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear the counters, references, history and any manual override."""
        self.state = self.state.cleared()
        self._sample_days = 0

    def set_consumption(
        self, total_litres: float, daily_litres: float | None = None
    ) -> None:
        """Set the totals by hand.

        `daily_litres` is a persistent override: it survives polls and
        restarts until a reset clears it.
        """
        self.state.total_litres = total_litres
        if daily_litres is not None:
            self.state.daily_override = daily_litres

    def rebase(self, published: dict[str, Any]) -> None:
        """Point the references at a reading without touching the totals.

        Only validated readings become references. A missing level used to be
        coerced to 0, which made the very next poll look like the tank had
        been filled from empty.
        """
        volume = volume_litres(published.get("usable_volume_litres"))
        level = percentage(published.get("total_level_percentage"))

        if volume is None and level is None:
            _LOGGER.warning(
                "Refusing to set a consumption reference from a reading with "
                "neither a volume nor a level"
            )
            return

        self.state.reference_volume = volume
        self.state.reference_level = level

    def apply(self, reading: TankReading, now: datetime) -> None:
        """Move the running totals on from a validated reading."""
        if self.state.reference_volume is None and self.state.reference_level is None:
            _LOGGER.info(
                "First update for this tank - setting initial references "
                "without calculating consumption"
            )
            self.rebase(reading.as_state())
            self.state.last_update = now
            return

        transition = consumption.classify(
            self.state.reference_volume, self.state.reference_level, reading
        )

        if transition.is_refill:
            _LOGGER.info(
                "Detected a tank refill of %s L from the %s",
                round(transition.litres_added, 1),
                transition.source,
            )
            # The next consumption interval starts from now.
            self.state.last_update = now
        elif transition.is_consumption:
            _LOGGER.info(
                "Detected consumption of %s L from the %s",
                round(transition.litres_consumed, 1),
                transition.source,
            )
            self._record(transition.litres_consumed, now)

        # Only advance a reference we actually have a fresh reading for. A
        # reading that carries a level but no volume must not blank the volume
        # reference, or the next poll would book the whole tank as consumed.
        if reading.volume_litres is not None:
            self.state.reference_volume = reading.volume_litres
        if reading.level_percentage is not None:
            self.state.reference_level = reading.level_percentage

    def _record(self, litres: float, now: datetime) -> None:
        """Record observed consumption and refresh the derived averages."""
        self.state.total_litres += litres
        self.state.history.extend(
            consumption.allocate_over_days(litres, self.state.last_update, now)
        )
        self._refresh_rolling_average(now)
        self.state.last_update = now

    def _refresh_rolling_average(self, now: datetime) -> dict[str, float]:
        """Recompute the rolling daily rate. Returns the regrouped totals."""
        totals = consumption.daily_totals(self.state.history)
        window = consumption.rolling_window(totals, now)
        self._sample_days = len(window)
        self.state.daily_litres = consumption.average(window) if window else None
        return totals

    def refresh_sample_count(self, now: datetime) -> None:
        """Recount the samples behind the measured rate (used after loading)."""
        self._sample_days = len(
            consumption.rolling_window(
                consumption.daily_totals(self.state.history), now
            )
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def decorate(
        self, published: dict[str, Any], kwh_per_litre: float
    ) -> dict[str, Any]:
        """Attach this tank's derived figures to a published state."""
        daily = self.daily_litres

        published["total_consumption_usable_liters"] = self.total_litres
        published["total_consumption_usable_kwh"] = self.total_kwh(kwh_per_litre)
        published["daily_consumption_usable_liters"] = daily
        published["consumption_sample_days"] = self.sample_days
        published["daily_consumption_is_manual"] = self.daily_is_manual
        published["days_until_empty"] = consumption.days_until_empty(
            published.get("current_volume_litres"),
            published.get("capacity_litres"),
            published.get("total_level_percentage"),
            daily or 0.0,
        )
        published["kwh_per_litre"] = kwh_per_litre
        return published

    def publish(
        self, reading: TankReading, now: datetime, kwh_per_litre: float
    ) -> dict[str, Any]:
        """Return the state dict for `reading`, with the derived figures."""
        published = reading.as_state()

        # Recalculate on every run, not just when consumption was detected,
        # so old incorrect data ages out after the rolling window.
        if self.state.history:
            totals = self._refresh_rolling_average(now)
            self.state.history = consumption.trim_history(totals, now, self._midnight)
            published["seasonal_stats"] = consumption.seasonal_stats(
                totals, now, self._midnight
            )
        else:
            published["seasonal_stats"] = {}

        return self.decorate(published, kwh_per_litre)
