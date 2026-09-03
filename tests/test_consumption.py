"""The consumption maths: allocation across days, rolling rate, seasons."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

from custom_components.boilerjuice.coordinator import (
    CONSUMPTION_ROLLING_DAYS,
    BoilerJuiceDataUpdateCoordinator,
)

from .helpers import make_entry

LONDON = ZoneInfo("Europe/London")


@pytest.fixture
async def engine(hass: HomeAssistant) -> BoilerJuiceDataUpdateCoordinator:
    """A coordinator used only for its consumption maths."""
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, make_entry(hass))
    yield coordinator
    await coordinator.async_close()


def days(coordinator) -> dict[str, float]:
    return coordinator._calculate_daily_totals_from_history()


async def test_a_drop_seen_within_a_day_lands_on_one_day(engine) -> None:
    engine._last_update = datetime(2026, 1, 10, 6, 0, tzinfo=LONDON)
    now = datetime(2026, 1, 10, 18, 0, tzinfo=LONDON)

    engine._spread_consumption_over_days(12.0, now)

    assert days(engine) == {"2026-01-10": 12.0}


async def test_a_drop_spanning_days_is_shared_by_time_in_each_day(engine) -> None:
    """Oil burnt while Home Assistant was down belongs to the days it spanned."""
    engine._last_update = datetime(2026, 1, 10, 12, 0, tzinfo=LONDON)
    now = datetime(2026, 1, 13, 12, 0, tzinfo=LONDON)

    engine._spread_consumption_over_days(120.0, now)

    allocated = days(engine)
    assert allocated == {
        "2026-01-10": pytest.approx(20.0),
        "2026-01-11": pytest.approx(40.0),
        "2026-01-12": pytest.approx(40.0),
        "2026-01-13": pytest.approx(20.0),
    }
    assert sum(allocated.values()) == pytest.approx(120.0)


async def test_the_shares_still_sum_across_a_spring_dst_boundary(engine) -> None:
    """The clocks going forward shortens a day; the total must still balance."""
    engine._last_update = datetime(2026, 3, 28, 12, 0, tzinfo=LONDON)
    now = datetime(2026, 3, 30, 12, 0, tzinfo=LONDON)

    engine._spread_consumption_over_days(96.0, now)

    allocated = days(engine)
    assert sorted(allocated) == ["2026-03-28", "2026-03-29", "2026-03-30"]
    assert sum(allocated.values()) == pytest.approx(96.0)


async def test_the_shares_still_sum_across_an_autumn_dst_boundary(engine) -> None:
    """The clocks going back lengthens a day; the total must still balance."""
    engine._last_update = datetime(2026, 10, 24, 12, 0, tzinfo=LONDON)
    now = datetime(2026, 10, 26, 12, 0, tzinfo=LONDON)

    engine._spread_consumption_over_days(96.0, now)

    allocated = days(engine)
    assert sorted(allocated) == ["2026-10-24", "2026-10-25", "2026-10-26"]
    assert sum(allocated.values()) == pytest.approx(96.0)


async def test_a_clock_that_went_backwards_lands_on_one_day(engine) -> None:
    engine._last_update = datetime(2026, 1, 12, 12, 0, tzinfo=LONDON)
    now = datetime(2026, 1, 10, 12, 0, tzinfo=LONDON)

    engine._spread_consumption_over_days(5.0, now)

    assert days(engine) == {"2026-01-10": 5.0}


async def test_the_rolling_rate_averages_complete_days_only(engine) -> None:
    """Today is still filling, so including it would drag the rate down."""
    engine._consumption_history_with_dates = [
        (datetime(2026, 1, 8, 0, 0, tzinfo=LONDON), 20.0),
        (datetime(2026, 1, 9, 0, 0, tzinfo=LONDON), 30.0),
        (datetime(2026, 1, 10, 3, 0, tzinfo=LONDON), 2.0),
    ]

    engine._refresh_rolling_average(datetime(2026, 1, 10, 6, 0, tzinfo=LONDON))

    assert engine.daily_consumption_usable_liters == 25.0


async def test_the_rolling_rate_uses_today_when_it_is_all_there_is(engine) -> None:
    engine._consumption_history_with_dates = [
        (datetime(2026, 1, 10, 3, 0, tzinfo=LONDON), 4.0),
    ]

    engine._refresh_rolling_average(datetime(2026, 1, 10, 6, 0, tzinfo=LONDON))

    assert engine.daily_consumption_usable_liters == 4.0


async def test_the_rolling_window_is_bounded(engine) -> None:
    engine._consumption_history_with_dates = [
        (datetime(2026, 1, day, 0, 0, tzinfo=LONDON), float(day))
        for day in range(1, 21)
    ]

    engine._refresh_rolling_average(datetime(2026, 2, 1, 0, 0, tzinfo=LONDON))

    assert len(engine._daily_consumption_history) == CONSUMPTION_ROLLING_DAYS
    assert engine._daily_consumption_history == [
        14.0,
        15.0,
        16.0,
        17.0,
        18.0,
        19.0,
        20.0,
    ]


async def test_no_history_means_a_zero_rate(engine) -> None:
    engine._refresh_rolling_average(datetime(2026, 1, 10, 6, 0, tzinfo=LONDON))

    assert engine.daily_consumption_usable_liters == 0.0


async def test_seasonal_stats_group_by_season_and_month(engine) -> None:
    engine._consumption_history_with_dates = [
        (datetime(2026, 1, 5, 0, 0, tzinfo=LONDON), 30.0),
        (datetime(2026, 1, 6, 0, 0, tzinfo=LONDON), 20.0),
        (datetime(2026, 4, 5, 0, 0, tzinfo=LONDON), 8.0),
        (datetime(2026, 7, 5, 0, 0, tzinfo=LONDON), 2.0),
        (datetime(2026, 10, 5, 0, 0, tzinfo=LONDON), 12.0),
    ]

    stats = engine._calculate_seasonal_stats()

    assert stats["winter_avg"] == 25.0
    assert stats["winter_min"] == 20.0
    assert stats["winter_max"] == 30.0
    assert stats["spring_avg"] == 8.0
    assert stats["summer_avg"] == 2.0
    assert stats["autumn_avg"] == 12.0
    assert stats["monthly"]["January"] == 25.0


async def test_seasonal_stats_are_empty_without_history(engine) -> None:
    assert engine._calculate_seasonal_stats() == {}


@pytest.mark.parametrize(
    ("month", "season"),
    [(1, "winter"), (4, "spring"), (7, "summer"), (10, "autumn"), (12, "winter")],
)
async def test_season_lookup(engine, month: int, season: str) -> None:
    assert engine._get_season(datetime(2026, month, 15, tzinfo=LONDON)) == season


async def test_days_until_empty_prefers_measured_consumption(engine) -> None:
    engine._daily_consumption_usable_liters = 10.0

    assert engine._calculate_days_until_empty({"current_volume_litres": 250}) == 25.0


async def test_days_until_empty_falls_back_to_the_real_capacity(engine) -> None:
    """The fallback assumes 2% of capacity a day, not a hard-coded 510 L tank."""
    assert (
        engine._calculate_days_until_empty(
            {
                "current_volume_litres": 500,
                "capacity_litres": 5000,
                "total_level_percentage": 10,
            }
        )
        == 5.0
    )


async def test_days_until_empty_is_unknown_without_a_volume(engine) -> None:
    assert engine._calculate_days_until_empty({"capacity_litres": 2500}) is None


async def test_days_until_empty_is_unknown_without_a_capacity(engine) -> None:
    assert engine._calculate_days_until_empty({"current_volume_litres": 500}) is None


async def test_naive_stored_timestamps_are_localized_not_reinterpreted(engine) -> None:
    """Pre-timezone installs wrote naive local wall-clock times."""
    naive = datetime(2026, 1, 10, 12, 0)

    localized = engine._as_local(naive)

    assert localized.tzinfo is not None
    assert localized.replace(tzinfo=None) == naive
