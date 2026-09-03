"""Stored consumption: per-account, versioned, validated, and recoverable."""

from __future__ import annotations

import pytest
from custom_components.boilerjuice.const import CONF_KWH_PER_LITRE, DOMAIN
from custom_components.boilerjuice.coordinator import BoilerJuiceDataUpdateCoordinator
from custom_components.boilerjuice.storage import (
    LEGACY_STORAGE_KEY,
    LEGACY_STORAGE_VERSION,
    STORAGE_VERSION,
    AccountState,
    ConsumptionState,
    ConsumptionStore,
    InvalidStoredData,
    document_from_account,
    document_from_state,
    state_from_document,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import make_entry, mock_site, tank_page, tracker_of


def entry_key(entry) -> str:
    return f"{DOMAIN}.{entry.entry_id}"


def stored(hass_storage, key: str, tank_id: str = "123456") -> dict:
    """Return one tank's sub-document out of an account's document."""
    return hass_storage[key]["data"]["tanks"][tank_id]


def legacy_document(**overrides) -> dict:
    document = {
        "total_consumption_liters": 340.0,
        "total_consumption_kwh": 3519.0,
        "daily_consumption_liters": 12.0,
        "reference_volume": 2000,
        "reference_level": 80.0,
        "consumption_history": [12.0, 12.0],
        "consumption_history_with_dates": [
            ["2026-01-08T00:00:00+00:00", 12.0],
            ["2026-01-09T00:00:00+00:00", 12.0],
        ],
        "last_update": "2026-01-09T06:00:00+00:00",
    }
    document.update(overrides)
    return document


# --- document validation --------------------------------------------------


def test_a_state_round_trips_through_its_document() -> None:
    state = ConsumptionState(total_litres=42.5, daily_litres=3.0, reference_level=61.0)

    assert state_from_document(document_from_state(state)) == state


@pytest.mark.parametrize(
    "document",
    [
        pytest.param("not an object", id="not-an-object"),
        pytest.param({"total_litres": "lots"}, id="total-not-a-number"),
        pytest.param({"total_litres": -5}, id="negative-total"),
        pytest.param({"total_litres": float("nan")}, id="nan-total"),
        pytest.param({"total_litres": float("inf")}, id="infinite-total"),
        pytest.param({"total_litres": 1e12}, id="absurd-total"),
        pytest.param({"reference_level": 140}, id="impossible-level"),
        pytest.param({"reference_volume": -1}, id="negative-volume"),
        pytest.param({"last_update": "yesterday"}, id="unreadable-timestamp"),
        pytest.param({"history": "some"}, id="history-not-a-list"),
        pytest.param({"history": [["2026-01-01T00:00:00+00:00"]]}, id="short-row"),
        pytest.param({"history": [["nope", 1.0]]}, id="bad-row-timestamp"),
        pytest.param(
            {"history": [["2026-01-01T00:00:00+00:00", "x"]]}, id="bad-row-litres"
        ),
        pytest.param({"daily_litres": True}, id="boolean-rate"),
    ],
)
def test_an_untrustworthy_document_is_refused(document: object) -> None:
    with pytest.raises(InvalidStoredData):
        state_from_document(document)


def test_history_is_sorted_on_the_way_in() -> None:
    state = state_from_document(
        {
            "history": [
                ["2026-01-09T00:00:00+00:00", 2.0],
                ["2026-01-08T00:00:00+00:00", 1.0],
            ]
        }
    )

    assert [litres for _, litres in state.history] == [1.0, 2.0]


def test_an_over_long_history_keeps_the_newest_rows() -> None:
    state = state_from_document(
        {
            "history": [
                [f"2026-01-01T00:00:{second:02d}+00:00", float(second)]
                for second in range(60)
            ]
            * 30
        }
    )

    assert len(state.history) == 1000


def test_a_naive_stored_timestamp_is_localized_not_reinterpreted() -> None:
    """Pre-timezone installs wrote naive local wall-clock times."""
    state = state_from_document({"last_update": "2026-01-10T12:00:00"})

    assert state.last_update.tzinfo is not None
    assert state.last_update.replace(tzinfo=None).hour == 12


# --- per-entry storage ----------------------------------------------------


async def test_each_account_writes_its_own_document(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    """The shared document is what let one account drop another's history."""
    first = make_entry(hass, email="one@example.com", tank_id="111111")
    second = make_entry(hass, email="two@example.com", tank_id="222222")
    one = BoilerJuiceDataUpdateCoordinator(hass, first)
    two = BoilerJuiceDataUpdateCoordinator(hass, second)

    try:
        mock_site(
            aioclient_mock,
            tank_html=tank_page(percentage=80, litres=2000),
            tank_id="111111",
        )
        mock_site(
            aioclient_mock,
            tank_html=tank_page(percentage=50, litres=1250),
            tank_id="222222",
            clear=False,
        )
        await one.async_refresh()
        await two.async_refresh()
        await hass.async_block_till_done()

        assert (
            stored(hass_storage, entry_key(first), "111111")["reference_volume"] == 2000
        )
        assert (
            stored(hass_storage, entry_key(second), "222222")["reference_volume"]
            == 1250
        )
    finally:
        await one.async_close()
        await two.async_close()


async def test_concurrent_polls_do_not_lose_each_others_writes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    import asyncio

    first = make_entry(hass, email="one@example.com", tank_id="111111")
    second = make_entry(hass, email="two@example.com", tank_id="222222")
    one = BoilerJuiceDataUpdateCoordinator(hass, first)
    two = BoilerJuiceDataUpdateCoordinator(hass, second)

    try:
        mock_site(
            aioclient_mock,
            tank_html=tank_page(percentage=80, litres=2000),
            tank_id="111111",
        )
        mock_site(
            aioclient_mock,
            tank_html=tank_page(percentage=50, litres=1250),
            tank_id="222222",
            clear=False,
        )
        await asyncio.gather(one.async_refresh(), two.async_refresh())
        await hass.async_block_till_done()

        assert (
            stored(hass_storage, entry_key(first), "111111")["reference_volume"] == 2000
        )
        assert (
            stored(hass_storage, entry_key(second), "222222")["reference_volume"]
            == 1250
        )
    finally:
        await one.async_close()
        await two.async_close()


async def test_a_restart_resumes_from_the_stored_totals(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    entry = make_entry(hass)
    hass_storage[entry_key(entry)] = {
        "version": STORAGE_VERSION,
        "data": document_from_account(
            AccountState(
                tanks={
                    "123456": ConsumptionState(
                        total_litres=340.0,
                        daily_litres=12.0,
                        reference_volume=2000,
                        reference_level=80.0,
                    )
                }
            )
        ),
    }
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=79, litres=1950))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # 340 carried over, plus the 50 L drop seen on this poll.
        assert tracker_of(coordinator).total_litres == 390.0
    finally:
        await coordinator.async_close()


# --- migration from the shared v1 document --------------------------------


async def test_an_entry_keyed_v1_slot_is_migrated(
    hass: HomeAssistant, hass_storage
) -> None:
    entry = make_entry(hass)
    hass_storage[LEGACY_STORAGE_KEY] = {
        "version": LEGACY_STORAGE_VERSION,
        "data": {entry.entry_id: legacy_document()},
    }
    store = ConsumptionStore(hass, entry.entry_id, "123456")

    account, problem = await store.async_load()

    assert problem is None
    assert account.unassigned.total_litres == 340.0
    assert account.unassigned.daily_litres == 12.0
    assert len(account.unassigned.history) == 2
    assert LEGACY_STORAGE_KEY not in hass_storage
    assert hass_storage[entry_key(entry)]["data"]["unassigned"]["total_litres"] == 340.0


async def test_a_tank_keyed_v1_slot_is_migrated(
    hass: HomeAssistant, hass_storage
) -> None:
    entry = make_entry(hass)
    hass_storage[LEGACY_STORAGE_KEY] = {
        "version": LEGACY_STORAGE_VERSION,
        "data": {"123456": legacy_document()},
    }
    store = ConsumptionStore(hass, entry.entry_id, "123456")

    account, _ = await store.async_load()

    assert account.tanks["123456"].total_litres == 340.0


async def test_the_v1_default_slot_is_only_claimed_with_a_tank_id(
    hass: HomeAssistant, hass_storage
) -> None:
    """With several untagged accounts the shared bucket is ambiguous."""
    entry = make_entry(hass, tank_id=None)
    hass_storage[LEGACY_STORAGE_KEY] = {
        "version": LEGACY_STORAGE_VERSION,
        "data": {"default": legacy_document()},
    }
    store = ConsumptionStore(hass, entry.entry_id, None)

    account, _ = await store.async_load()

    assert account.tanks == {}
    assert account.unassigned is None
    assert "default" in hass_storage[LEGACY_STORAGE_KEY]["data"]


async def test_migrating_one_account_leaves_the_others_slots_alone(
    hass: HomeAssistant, hass_storage
) -> None:
    entry = make_entry(hass)
    hass_storage[LEGACY_STORAGE_KEY] = {
        "version": LEGACY_STORAGE_VERSION,
        "data": {"123456": legacy_document(), "999999": legacy_document()},
    }
    store = ConsumptionStore(hass, entry.entry_id, "123456")

    await store.async_load()

    assert list(hass_storage[LEGACY_STORAGE_KEY]["data"]) == ["999999"]


async def test_a_v1_zero_daily_rate_becomes_unknown_not_zero(
    hass: HomeAssistant, hass_storage
) -> None:
    """v1 wrote 0.0 for "not measured yet", which is not a real rate."""
    entry = make_entry(hass)
    hass_storage[LEGACY_STORAGE_KEY] = {
        "version": LEGACY_STORAGE_VERSION,
        "data": {"123456": legacy_document(daily_consumption_liters=0.0)},
    }
    store = ConsumptionStore(hass, entry.entry_id, "123456")

    account, _ = await store.async_load()

    assert account.tanks["123456"].daily_litres is None


async def test_an_unusable_v1_slot_is_discarded_and_reported(
    hass: HomeAssistant, hass_storage
) -> None:
    entry = make_entry(hass)
    hass_storage[LEGACY_STORAGE_KEY] = {
        "version": LEGACY_STORAGE_VERSION,
        "data": {"123456": legacy_document(total_consumption_liters="lots")},
    }
    store = ConsumptionStore(hass, entry.entry_id, "123456")

    account, problem = await store.async_load()

    assert account.tanks == {}
    assert problem is not None


# --- recovery -------------------------------------------------------------


async def test_a_corrupt_document_starts_fresh_and_raises_a_repair(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    entry = make_entry(hass)
    hass_storage[entry_key(entry)] = {
        "version": STORAGE_VERSION,
        "data": {"tanks": {"123456": {"total_litres": "three hundred"}}},
    }
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.last_update_success
        assert tracker_of(coordinator).total_litres == 0.0

        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"invalid_stored_data_{entry.entry_id}"
        )
        assert issue is not None
        assert issue.translation_key == "invalid_stored_data"
    finally:
        await coordinator.async_close()


async def test_healthy_storage_raises_no_repair(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = make_entry(hass)
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert (
            ir.async_get(hass).async_get_issue(
                DOMAIN, f"invalid_stored_data_{entry.entry_id}"
            )
            is None
        )
    finally:
        await coordinator.async_close()


async def test_removing_an_entry_deletes_its_document(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    entry = make_entry(hass)
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert entry_key(entry) in hass_storage

        await coordinator.async_remove_storage()
        await hass.async_block_till_done()

        assert entry_key(entry) not in hass_storage
    finally:
        await coordinator.async_close()


async def test_reset_clears_the_stored_document(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    entry = make_entry(hass)
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        mock_site(aioclient_mock, tank_html=tank_page(percentage=70, litres=1750))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert tracker_of(coordinator).total_litres == 250.0

        await coordinator.async_reset_consumption()

        document = stored(hass_storage, entry_key(entry))
        assert document["total_litres"] == 0.0
        assert document["reference_volume"] is None
        assert document["history"] == []
    finally:
        await coordinator.async_close()


async def test_a_manual_daily_rate_survives_the_next_poll_and_a_restart(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    """The override is documented as persistent, not silently recomputed."""
    entry = make_entry(hass)
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await coordinator.async_set_consumption(100.0, 7.5)
        await hass.async_block_till_done()

        mock_site(aioclient_mock, tank_html=tank_page(percentage=70, litres=1750))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert tracker_of(coordinator).daily_litres == 7.5
        assert tracker_of(coordinator).daily_is_manual
        assert stored(hass_storage, entry_key(entry))["daily_override"] == 7.5
    finally:
        await coordinator.async_close()

    restarted = BoilerJuiceDataUpdateCoordinator(hass, entry)
    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=70, litres=1750))
        await restarted.async_refresh()
        await hass.async_block_till_done()

        assert tracker_of(restarted).daily_litres == 7.5
    finally:
        await restarted.async_close()


async def test_resetting_clears_a_manual_daily_rate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = make_entry(hass)
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await coordinator.async_set_consumption(100.0, 7.5)
        await coordinator.async_reset_consumption()

        assert tracker_of(coordinator).daily_litres is None
        assert not tracker_of(coordinator).daily_is_manual
    finally:
        await coordinator.async_close()


async def test_a_run_of_unreadable_pages_raises_a_repair(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A persistent layout change needs the user to look for an update."""
    from custom_components.boilerjuice.coordinator import PARSE_FAILURES_BEFORE_REPAIR

    from .helpers import load_fixture

    entry = make_entry(hass)
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)
    issue_id = f"page_layout_changed_{entry.entry_id}"

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        mock_site(aioclient_mock, tank_html=load_fixture("tank_redesigned.html"))
        for _ in range(PARSE_FAILURES_BEFORE_REPAIR - 1):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

        # And it clears as soon as a page parses again.
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    finally:
        await coordinator.async_close()


async def test_a_transient_storage_failure_does_not_lose_the_history(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    """Marking storage loaded before the read succeeded destroyed history.

    The flag was set up front, so a single failed read was remembered as "we
    have loaded"; the next refresh started from empty history and the first
    save overwrote a perfectly good document.
    """
    from unittest.mock import patch

    entry = make_entry(hass)
    hass_storage[entry_key(entry)] = {
        "version": STORAGE_VERSION,
        "data": document_from_account(
            AccountState(tanks={"123456": ConsumptionState(total_litres=340.0)})
        ),
    }
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))

        with patch(
            "custom_components.boilerjuice.storage.ConsumptionStore.async_load",
            side_effect=OSError("disk hiccup"),
        ):
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        assert not coordinator.last_update_success
        # Nothing was written over the top of the good document.
        assert stored(hass_storage, entry_key(entry))["total_litres"] == 340.0

        # The next refresh tries the read again rather than assuming it ran.
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.last_update_success
        assert tracker_of(coordinator).total_litres == 340.0
    finally:
        await coordinator.async_close()


async def test_changing_the_energy_content_does_not_rewrite_past_energy(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The energy sensor is TOTAL_INCREASING, so history must not move.

    Recomputing the whole total with a new factor shows up in long-term
    statistics as a jump, or as a meter reset when the factor goes down.
    """
    entry = make_entry(hass, **{CONF_KWH_PER_LITRE: 10.0})
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        mock_site(aioclient_mock, tank_html=tank_page(percentage=79, litres=1900))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        tracker = tracker_of(coordinator)
        assert tracker.total_litres == 100.0
        assert tracker.total_kwh(10.0) == pytest.approx(1000.0)

        # The user corrects the factor. The 100 L already burnt keep the
        # energy they were recorded with.
        coordinator._kwh_per_litre = 5.0
        mock_site(aioclient_mock, tank_html=tank_page(percentage=78, litres=1800))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert tracker.total_litres == 200.0
        # 100 L at 10.0 plus 100 L at 5.0, not 200 L at 5.0.
        assert tracker.total_kwh(5.0) == pytest.approx(1500.0)
    finally:
        await coordinator.async_close()


async def test_energy_carries_across_from_the_v1_document(
    hass: HomeAssistant, hass_storage
) -> None:
    """v1 stored energy, so upgrading must not restate it."""
    entry = make_entry(hass)
    hass_storage[LEGACY_STORAGE_KEY] = {
        "version": LEGACY_STORAGE_VERSION,
        "data": {"123456": legacy_document()},
    }
    store = ConsumptionStore(hass, entry.entry_id, "123456")

    account, _ = await store.async_load()

    assert account.tanks["123456"].total_kwh == 3519.0


async def test_a_document_without_energy_is_seeded_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A document written before energy was stored gets a starting point."""
    entry = make_entry(hass, **{CONF_KWH_PER_LITRE: 9.0})
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        tracker = tracker_of(coordinator)
        tracker.state.total_litres = 100.0
        tracker.state.total_kwh = None

        assert tracker.total_kwh(9.0) == pytest.approx(900.0)
    finally:
        await coordinator.async_close()


async def test_setting_the_total_by_hand_rebases_the_energy(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Stating a total is a deliberate act, so a statistics jump is expected."""
    entry = make_entry(hass, **{CONF_KWH_PER_LITRE: 9.6})
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await coordinator.async_set_consumption(100.0)
        await hass.async_block_till_done()

        assert tracker_of(coordinator).total_kwh(9.6) == pytest.approx(960.0)
    finally:
        await coordinator.async_close()
