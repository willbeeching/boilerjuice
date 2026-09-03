"""The consumption maths, tested as the pure functions they now are."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from custom_components.boilerjuice import consumption
from custom_components.boilerjuice.models import TankReading

LONDON = ZoneInfo("Europe/London")


def at(day: int, hour: int = 12, month: int = 1) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=LONDON)


def midnight(date_str: str) -> datetime:
    return datetime.fromisoformat(date_str).replace(tzinfo=LONDON)


# --- allocation -----------------------------------------------------------


def test_a_drop_seen_within_a_day_lands_on_one_day() -> None:
    allocated = consumption.allocate_over_days(12.0, at(10, 6), at(10, 18))

    assert consumption.daily_totals(allocated) == {"2026-01-10": 12.0}


def test_a_drop_with_no_baseline_lands_on_now() -> None:
    allocated = consumption.allocate_over_days(12.0, None, at(10, 18))

    assert consumption.daily_totals(allocated) == {"2026-01-10": 12.0}


def test_a_drop_spanning_days_is_shared_by_time_in_each_day() -> None:
    """Oil burnt while Home Assistant was down belongs to the days it spanned."""
    allocated = consumption.daily_totals(
        consumption.allocate_over_days(120.0, at(10), at(13))
    )

    assert allocated == {
        "2026-01-10": pytest.approx(20.0),
        "2026-01-11": pytest.approx(40.0),
        "2026-01-12": pytest.approx(40.0),
        "2026-01-13": pytest.approx(20.0),
    }
    assert sum(allocated.values()) == pytest.approx(120.0)


@pytest.mark.parametrize(
    ("month", "start_day", "end_day"),
    [
        pytest.param(3, 28, 30, id="clocks-forward"),
        pytest.param(10, 24, 26, id="clocks-back"),
    ],
)
def test_the_shares_still_sum_across_a_dst_boundary(
    month: int, start_day: int, end_day: int
) -> None:
    """A 23- or 25-hour day must not gain or lose oil."""
    allocated = consumption.daily_totals(
        consumption.allocate_over_days(
            96.0, at(start_day, month=month), at(end_day, month=month)
        )
    )

    assert len(allocated) == 3
    assert sum(allocated.values()) == pytest.approx(96.0)


def test_a_clock_that_went_backwards_lands_on_one_day() -> None:
    allocated = consumption.allocate_over_days(5.0, at(12), at(10))

    assert consumption.daily_totals(allocated) == {"2026-01-10": 5.0}


def test_a_drop_across_midnight_is_split_between_the_two_days() -> None:
    """The shortcut is by calendar date, not by "under 24 hours".

    Testing the duration put a 23:30 to 00:30 drop entirely on the second
    day, which misallocates the busiest hours of a winter evening.
    """
    since = datetime(2026, 1, 10, 23, 30, tzinfo=LONDON)
    now = datetime(2026, 1, 11, 0, 30, tzinfo=LONDON)

    allocated = consumption.daily_totals(
        consumption.allocate_over_days(4.0, since, now)
    )

    assert allocated == {
        "2026-01-10": pytest.approx(2.0),
        "2026-01-11": pytest.approx(2.0),
    }


def test_an_overnight_drop_is_split_by_time_in_each_day() -> None:
    since = datetime(2026, 1, 10, 18, 0, tzinfo=LONDON)
    now = datetime(2026, 1, 11, 6, 0, tzinfo=LONDON)

    allocated = consumption.daily_totals(
        consumption.allocate_over_days(12.0, since, now)
    )

    assert allocated == {
        "2026-01-10": pytest.approx(6.0),
        "2026-01-11": pytest.approx(6.0),
    }
    assert sum(allocated.values()) == pytest.approx(12.0)


def test_two_samples_on_the_same_day_are_not_split() -> None:
    allocated = consumption.allocate_over_days(4.0, at(10, 1), at(10, 23))

    assert consumption.daily_totals(allocated) == {"2026-01-10": 4.0}


# --- rolling rate ---------------------------------------------------------


def test_the_rolling_rate_averages_complete_days_only() -> None:
    """Today is still filling, so including it would drag the rate down."""
    totals = consumption.daily_totals(
        [(at(8, 0), 20.0), (at(9, 0), 30.0), (at(10, 3), 2.0)]
    )

    window = consumption.rolling_window(totals, at(10, 6))

    assert consumption.average(window) == 25.0


def test_the_rolling_rate_uses_today_when_it_is_all_there_is() -> None:
    totals = consumption.daily_totals([(at(10, 3), 4.0)])

    assert consumption.average(consumption.rolling_window(totals, at(10, 6))) == 4.0


def test_the_rolling_window_is_bounded() -> None:
    totals = consumption.daily_totals(
        [(at(day, 0), float(day)) for day in range(1, 21)]
    )

    window = consumption.rolling_window(totals, datetime(2026, 2, 1, tzinfo=LONDON))

    assert window == [14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0]


def test_no_history_means_a_zero_rate() -> None:
    assert consumption.average(consumption.rolling_window({}, at(10))) == 0.0


def test_history_is_trimmed_to_just_over_a_year() -> None:
    totals = {"2024-01-01": 10.0, "2026-01-09": 5.0}

    trimmed = consumption.trim_history(totals, at(10), midnight)

    assert [date.date().isoformat() for date, _ in trimmed] == ["2026-01-09"]


# --- transitions ----------------------------------------------------------


def reading(**kwargs) -> TankReading:
    return TankReading(tank_id="123456", **kwargs)


def test_a_volume_drop_is_consumption() -> None:
    transition = consumption.classify(2000, 80, reading(volume_litres=1975))

    assert transition.is_consumption
    assert transition.litres_consumed == 25
    assert transition.source == "volume"


def test_a_volume_rise_is_a_refill() -> None:
    transition = consumption.classify(500, 20, reading(volume_litres=2375))

    assert transition.is_refill
    assert not transition.is_consumption


def test_an_unchanged_volume_is_neither() -> None:
    transition = consumption.classify(2000, 80, reading(volume_litres=2000))

    assert not transition.is_consumption
    assert not transition.is_refill


def test_the_level_is_used_when_there_is_no_volume() -> None:
    transition = consumption.classify(
        None, 80, reading(level_percentage=79, capacity_litres=2500)
    )

    assert transition.litres_consumed == pytest.approx(25.0)
    assert transition.source == "level"


def test_a_level_rise_is_a_refill() -> None:
    transition = consumption.classify(
        None, 20, reading(level_percentage=95, capacity_litres=2500)
    )

    assert transition.is_refill


def test_a_level_without_a_capacity_yields_nothing() -> None:
    """No capacity means no way to turn a percentage into litres."""
    transition = consumption.classify(None, 80, reading(level_percentage=40))

    assert transition == consumption.UNCHANGED


def test_no_previous_reference_yields_nothing() -> None:
    transition = consumption.classify(None, None, reading(volume_litres=1000))

    assert transition == consumption.UNCHANGED


# --- seasons --------------------------------------------------------------


def test_seasonal_stats_group_by_season_and_month() -> None:
    totals = {
        "2026-01-05": 30.0,
        "2026-01-06": 20.0,
        "2026-04-05": 8.0,
        "2026-07-05": 2.0,
        "2026-10-05": 12.0,
    }

    stats = consumption.seasonal_stats(totals, at(10), midnight)

    assert stats["winter_avg"] == 25.0
    assert stats["winter_min"] == 20.0
    assert stats["winter_max"] == 30.0
    assert stats["spring_avg"] == 8.0
    assert stats["summer_avg"] == 2.0
    assert stats["autumn_avg"] == 12.0
    assert stats["monthly"]["January"] == 25.0
    assert stats["current_season"]["name"] == "winter"


def test_seasonal_stats_are_empty_without_history() -> None:
    assert consumption.seasonal_stats({}, at(10), midnight) == {}


def test_a_season_with_no_data_is_unknown_not_a_measured_zero() -> None:
    """0.0 L/day would claim the tank measurably burnt nothing all winter."""
    stats = consumption.seasonal_stats({"2026-07-05": 2.0}, at(10), midnight)

    assert stats["current_season"] == {
        "name": None,
        "avg": None,
        "min": None,
        "max": None,
    }
    assert "winter_avg" not in stats


@pytest.mark.parametrize(
    ("month", "season"),
    [(1, "winter"), (4, "spring"), (7, "summer"), (10, "autumn"), (12, "winter")],
)
def test_season_lookup(month: int, season: str) -> None:
    assert consumption.season_for(datetime(2026, month, 15, tzinfo=LONDON)) == season


# --- days until empty -----------------------------------------------------


def test_days_until_empty_prefers_measured_consumption() -> None:
    assert consumption.days_until_empty(250, 2500, 10, 10.0) == 25.0


def test_days_until_empty_falls_back_to_the_real_capacity() -> None:
    """The fallback assumes 2% of capacity a day, not a hard-coded 510 L tank."""
    assert consumption.days_until_empty(500, 5000, 10, 0.0) == 5.0


def test_days_until_empty_is_unknown_without_a_volume() -> None:
    assert consumption.days_until_empty(None, 2500, 50, 0.0) is None


def test_days_until_empty_is_unknown_without_a_capacity() -> None:
    assert consumption.days_until_empty(500, None, 50, 0.0) is None


def test_days_until_empty_is_unknown_at_zero_percent() -> None:
    assert consumption.days_until_empty(500, 2500, 0, 0.0) is None
