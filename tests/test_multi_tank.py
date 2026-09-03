"""One account, several tanks: discovery, removal and per-tank targeting."""

from __future__ import annotations

import pytest
from custom_components.boilerjuice import SERVICE_RESET_CONSUMPTION
from custom_components.boilerjuice.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TANKS,
    DOMAIN,
    LOGIN_URL,
    PRICE_URL,
    TANKS_URL,
)
from custom_components.boilerjuice.coordinator import MISSING_LISTINGS_BEFORE_REMOVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import (
    PRICE_PAGE,
    SIGNED_IN_PAGE,
    coordinator_of,
    load_fixture,
    tank_page,
    tracker_of,
)

FIRST = "123456"
SECOND = "789012"

ONE_TANK = f'<a href="/uk/users/tanks/{FIRST}/edit">One</a>'
TWO_TANKS = ONE_TANK + f'<a href="/uk/users/tanks/{SECOND}/edit">Two</a>'


def mock_account(
    aioclient_mock: AiohttpClientMocker, listing: str, *, clear: bool = True
) -> None:
    """Register an account whose tanks page lists `listing`."""
    if clear:
        aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=f"<html><body>{listing}</body></html>")
    aioclient_mock.get(
        f"{TANKS_URL}/{FIRST}/edit", text=tank_page(percentage=80, litres=2000)
    )
    aioclient_mock.get(
        f"{TANKS_URL}/{SECOND}/edit", text=tank_page(percentage=40, litres=900)
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)


@pytest.fixture
async def account(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> MockConfigEntry:
    """Return an unpinned account that starts out with two tanks."""
    mock_account(aioclient_mock, TWO_TANKS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_ids(hass: HomeAssistant, tank_hint: str) -> list[str]:
    return [
        state.entity_id
        for state in hass.states.async_all("sensor")
        if tank_hint in state.entity_id
    ]


async def test_both_tanks_get_their_own_device_and_entities(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    coordinator = coordinator_of(account)
    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]

    registry = dr.async_get(hass)
    assert registry.async_get_device(identifiers={(DOMAIN, FIRST)}) is not None
    assert registry.async_get_device(identifiers={(DOMAIN, SECOND)}) is not None

    # 14 sensors per tank, and nothing shared between them.
    assert len(hass.states.async_all("sensor")) == 28


async def test_each_tank_keeps_its_own_consumption(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    coordinator = coordinator_of(account)

    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=f"<html><body>{TWO_TANKS}</body></html>")
    aioclient_mock.get(
        f"{TANKS_URL}/{FIRST}/edit", text=tank_page(percentage=79, litres=1950)
    )
    aioclient_mock.get(
        f"{TANKS_URL}/{SECOND}/edit", text=tank_page(percentage=40, litres=900)
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert tracker_of(coordinator, FIRST).total_litres == 50.0
    assert tracker_of(coordinator, SECOND).total_litres == 0.0


async def test_a_tank_added_later_gets_entities_without_a_restart(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_account(aioclient_mock, ONE_TANK)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(hass.states.async_all("sensor")) == 14

    mock_account(aioclient_mock, TWO_TANKS)
    await coordinator_of(entry).async_refresh()
    await hass.async_block_till_done()

    assert len(hass.states.async_all("sensor")) == 28


async def test_a_tank_is_only_removed_after_repeated_authoritative_absences(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    coordinator = coordinator_of(account)
    mock_account(aioclient_mock, ONE_TANK)

    for poll in range(1, MISSING_LISTINGS_BEFORE_REMOVAL):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert SECOND in coordinator.tank_ids, f"removed after only {poll} listings"

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.tank_ids == [FIRST]


async def test_a_tank_that_comes_back_is_not_removed(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    coordinator = coordinator_of(account)

    mock_account(aioclient_mock, ONE_TANK)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    mock_account(aioclient_mock, TWO_TANKS)
    for _ in range(MISSING_LISTINGS_BEFORE_REMOVAL + 1):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]


async def test_a_failed_listing_never_removes_a_tank(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An outage must not delete anybody's devices."""
    coordinator = coordinator_of(account)

    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, status=503, text="")

    for _ in range(MISSING_LISTINGS_BEFORE_REMOVAL + 2):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]
    registry = dr.async_get(hass)
    assert registry.async_get_device(identifiers={(DOMAIN, SECOND)}) is not None


async def test_only_the_included_tanks_are_tracked(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_account(aioclient_mock, TWO_TANKS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        options={CONF_TANKS: [SECOND]},
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert coordinator_of(entry).tank_ids == [SECOND]


async def test_a_device_target_resets_only_that_tank(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    coordinator = coordinator_of(account)
    tracker_of(coordinator, FIRST).state.total_litres = 40.0
    tracker_of(coordinator, SECOND).state.total_litres = 90.0

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, FIRST)})
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"device_id": device.id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert tracker_of(coordinator, FIRST).total_litres == 0.0
    assert tracker_of(coordinator, SECOND).total_litres == 90.0


async def test_an_entry_target_resets_every_tank_on_the_account(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    coordinator = coordinator_of(account)
    tracker_of(coordinator, FIRST).state.total_litres = 40.0
    tracker_of(coordinator, SECOND).state.total_litres = 90.0

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"entry_id": account.entry_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert tracker_of(coordinator, FIRST).total_litres == 0.0
    assert tracker_of(coordinator, SECOND).total_litres == 0.0
