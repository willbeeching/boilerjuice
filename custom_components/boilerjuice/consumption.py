"""The consumption maths.

Pure functions over readings, timestamps and stored history: no network, no
Home Assistant, no mutation of anything the caller did not hand in. The
coordinator decides when to apply the results; this module only computes.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import TankReading

# Number of days to keep in the rolling average.
CONSUMPTION_ROLLING_DAYS = 7

# Dated history is kept for just over a year so every season has data to
# average. Entries are collapsed to one per day, so this bounds the stored
# history at ~400 rows.
SEASONAL_HISTORY_DAYS = 400

WINTER_MONTHS = (12, 1, 2)
SPRING_MONTHS = (3, 4, 5)
SUMMER_MONTHS = (6, 7, 8)
AUTUMN_MONTHS = (9, 10, 11)

SEASONS = ("winter", "spring", "summer", "autumn")

# Without measured consumption, assume a day burns this share of the tank.
FALLBACK_DAILY_SHARE_OF_CAPACITY = 0.02

DatedHistory = list[tuple[datetime, float]]


@dataclass(frozen=True, slots=True)
class Transition:
    """What changed between the previous references and a new reading."""

    litres_consumed: float = 0.0
    litres_added: float = 0.0
    source: str | None = None

    @property
    def is_consumption(self) -> bool:
        """Whether oil was burnt between the two readings."""
        return self.litres_consumed > 0

    @property
    def is_refill(self) -> bool:
        """Whether the tank gained oil between the two readings."""
        return self.litres_added > 0


UNCHANGED = Transition()


def elapsed_seconds(later: datetime, earlier: datetime) -> float:
    """Return the real seconds between two aware datetimes.

    Both are converted to UTC first. Subtracting two aware datetimes that
    share a tzinfo object makes CPython ignore the zone and subtract them as
    if they were naive, which returns wall-clock time: the 23-hour day when
    the clocks go forward measures 24 hours, and the 25-hour day measures 24
    as well. That is exactly the arithmetic the day-weighting must not do.
    """
    return (later.astimezone(UTC) - earlier.astimezone(UTC)).total_seconds()


def season_for(moment: datetime) -> str:
    """Return the season a date falls in."""
    month = moment.month
    if month in WINTER_MONTHS:
        return "winter"
    if month in SPRING_MONTHS:
        return "spring"
    if month in SUMMER_MONTHS:
        return "summer"
    return "autumn"


def classify(
    previous_volume: float | None,
    previous_level: float | None,
    reading: TankReading,
) -> Transition:
    """Describe the move from the previous references to `reading`.

    The direct volume change is the more precise signal and wins when both
    are available. The percentage only becomes litres when the page also gave
    us a capacity, so a partial page contributes nothing rather than
    contributing a guess.
    """
    volume = reading.volume_litres
    if volume is not None and previous_volume is not None:
        if volume > previous_volume:
            return Transition(litres_added=volume - previous_volume, source="volume")
        if volume < previous_volume:
            return Transition(litres_consumed=previous_volume - volume, source="volume")
        return UNCHANGED

    level = reading.level_percentage
    capacity = reading.capacity_litres
    if level is not None and previous_level is not None and capacity is not None:
        if level > previous_level:
            return Transition(
                litres_added=(level - previous_level) / 100 * capacity, source="level"
            )
        if level < previous_level:
            return Transition(
                litres_consumed=(previous_level - level) / 100 * capacity,
                source="level",
            )

    return UNCHANGED


def allocate_over_days(
    litres: float, since: datetime | None, now: datetime
) -> DatedHistory:
    """Apportion `litres` across the days it was actually burnt over.

    Consumption is only ever observed as a drop between two polls, so oil
    burnt while Home Assistant was down (or between sparse tank readings)
    belongs to the days it spanned rather than to the day we noticed it.

    Each calendar day is weighted by the real time the interval spent inside
    it, so the shares sum back to exactly `litres` and a daylight-saving day
    gets the share its 23 or 25 hours earned rather than a flat 24.

    The shortcut is taken on the calendar date, not on the length of the
    interval. Testing "under 24 hours" put a 23:30 to 00:30 drop entirely on
    the second day, which is a real misallocation on the busiest hours of a
    winter evening.
    """
    if since is None:
        return [(now, litres)]

    interval_seconds = elapsed_seconds(now, since)
    if interval_seconds <= 0:
        # The clock went backwards. There is no interval to spread over.
        return [(now, litres)]
    if since.date() == now.date():
        # Both samples fall on one calendar day, so there is nothing to split.
        return [(now, litres)]

    allocated: DatedHistory = []
    day = since.date()
    last_day = now.date()
    while day <= last_day:
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=since.tzinfo)
        day_end = day_start + timedelta(days=1)
        overlap = elapsed_seconds(min(now, day_end), max(since, day_start))
        if overlap > 0:
            allocated.append((day_start, litres * overlap / interval_seconds))
        day += timedelta(days=1)
    return allocated


def daily_totals(history: DatedHistory) -> dict[str, float]:
    """Group dated consumption into one total per calendar day."""
    totals: dict[str, float] = {}
    for moment, litres in history:
        key = moment.date().isoformat()
        totals[key] = totals.get(key, 0.0) + litres
    return dict(sorted(totals.items()))


def rolling_window(
    totals: dict[str, float], now: datetime, window: int = CONSUMPTION_ROLLING_DAYS
) -> list[float]:
    """Return the daily figures the rolling rate should average.

    Today's bucket is still filling, so averaging it alongside finished days
    drags the rate down: a day that is three hours old contributes three
    hours of oil but a full day of weight. Earlier days do fill in, because
    the next detection interval starts where the last one ended and tops up
    that day's bucket. On a fresh install today may be all there is, and
    reporting a partial day beats reporting nothing.
    """
    today = now.date().isoformat()
    complete_days = [litres for date, litres in totals.items() if date < today]
    return (complete_days or list(totals.values()))[-window:]


def average(values: list[float]) -> float:
    """Return the mean of `values`, or 0.0 when there are none."""
    return sum(values) / len(values) if values else 0.0


def trim_history(
    totals: dict[str, float],
    now: datetime,
    midnight: Callable[[str], datetime],
) -> DatedHistory:
    """Collapse history to one entry per day and drop anything too old.

    Seasonal averages need every season represented, so a short window would
    leave three of the four empty. The daily rollup is what every consumer
    reads anyway, so this loses nothing and bounds the stored rows.
    """
    cutoff = now - timedelta(days=SEASONAL_HISTORY_DAYS)
    return [
        (day_start, litres)
        for day_start, litres in (
            (midnight(date), litres) for date, litres in totals.items()
        )
        if day_start >= cutoff
    ]


def seasonal_stats(
    totals: dict[str, float],
    now: datetime,
    midnight: Callable[[str], datetime],
) -> dict[str, Any]:
    """Summarise consumption by season and by month."""
    if not totals:
        return {}

    stats: dict[str, Any] = {season: [] for season in SEASONS}
    stats["monthly"] = {}
    # None, not 0.0: a season we have never seen is unknown, and publishing
    # zero would claim the tank measurably burnt nothing all winter.
    stats["current_season"] = {"avg": None, "min": None, "max": None}

    for date, litres in totals.items():
        moment = midnight(date)
        stats[season_for(moment)].append(litres)
        stats["monthly"].setdefault(moment.strftime("%B"), []).append(litres)

    for season in SEASONS:
        if stats[season]:
            stats[f"{season}_avg"] = round(statistics.mean(stats[season]), 1)
            stats[f"{season}_min"] = round(min(stats[season]), 1)
            stats[f"{season}_max"] = round(max(stats[season]), 1)

    for month, values in stats["monthly"].items():
        stats["monthly"][month] = round(statistics.mean(values), 1)

    current = season_for(now)
    if stats[current]:
        stats["current_season"] = {
            "avg": round(statistics.mean(stats[current]), 1),
            "min": round(min(stats[current]), 1),
            "max": round(max(stats[current]), 1),
        }

    return stats


def days_until_empty(
    volume_litres: int | None,
    capacity_litres: int | None,
    level_percentage: float | None,
    daily_litres: float,
) -> float | None:
    """Estimate how long the remaining oil lasts."""
    if volume_litres is None:
        return None

    if daily_litres > 0:
        return round(volume_litres / daily_litres, 1)

    if capacity_litres and level_percentage is not None and level_percentage > 0:
        return round(
            volume_litres / (capacity_litres * FALLBACK_DAILY_SHARE_OF_CAPACITY), 1
        )

    return None
