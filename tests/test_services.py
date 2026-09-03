"""Reset and set_consumption rewrite stored history, so targeting must be exact.

The resolver used to advertise entity, area and label targets but only read
device_id and entry_id. Anything else fell through to "every configured
account", which silently wiped the other tank's history.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.boilerjuice import (
    SERVICE_RESET_CONSUMPTION,
    SERVICE_SET_CONSUMPTION,
)
from custom_components.boilerjuice.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TANK_ID,
    DOMAIN,
    LOGIN_URL,
    PRICE_URL,
    TANKS_URL,
)

from .conftest import load_fixture
from .test_coordinator import PRICE_PAGE, SIGNED_IN_PAGE, tank_page


async def add_account(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    *,
    email: str,
    tank_id: str,
    litres: int,
) -> MockConfigEntry:
    """Set up one fully-loaded BoilerJuice account."""
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)
    aioclient_mock.get(
        f"{TANKS_URL}/{tank_id}/edit", text=tank_page(percentage=80, litres=litres)
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Tank {tank_id}",
        data={
            CONF_EMAIL: email,
            CONF_PASSWORD: "hunter2",
            CONF_TANK_ID: tank_id,
        },
        unique_id=email,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def two_accounts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> tuple[MockConfigEntry, MockConfigEntry]:
    """Two configured accounts, each with recorded consumption."""
    first = await add_account(
        hass, aioclient_mock, email="one@example.com", tank_id="111111", litres=2000
    )
    second = await add_account(
        hass, aioclient_mock, email="two@example.com", tank_id="222222", litres=1500
    )

    for entry, total in ((first, 40.0), (second, 90.0)):
        coordinator = hass.data[DOMAIN][entry.entry_id]
        coordinator._total_consumption_usable_liters = total
        coordinator._total_consumption_usable_kwh = total * coordinator.kwh_per_litre

    return first, second


def totals(hass: HomeAssistant, *entries: MockConfigEntry) -> list[float]:
    """Return each entry's recorded total consumption."""
    return [
        hass.data[DOMAIN][entry.entry_id].total_consumption_usable_liters
        for entry in entries
    ]


async def test_an_unresolvable_target_is_refused_not_broadcast(
    hass: HomeAssistant, two_accounts
) -> None:
    """An area holding no BoilerJuice tank must not mean "reset everything"."""
    first, second = two_accounts

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"area_id": "kitchen"},
            blocking=True,
        )

    assert totals(hass, first, second) == [40.0, 90.0]


async def test_no_target_with_several_accounts_is_refused(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_RESET_CONSUMPTION, {}, blocking=True
        )

    assert totals(hass, first, second) == [40.0, 90.0]


async def test_a_device_target_reaches_only_that_account(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, "111111")})
    assert device is not None

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"device_id": device.id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, second) == [90.0]


async def test_an_entity_target_reaches_only_that_account(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts
    entity_registry = er.async_get(hass)
    entity_ids = [
        entity.entity_id
        for entity in er.async_entries_for_config_entry(entity_registry, first.entry_id)
    ]
    assert entity_ids

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"entity_id": entity_ids[0]},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, second) == [90.0]


async def test_an_unknown_entity_target_is_refused(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"entity_id": "sensor.does_not_exist"},
            blocking=True,
        )

    assert totals(hass, first, second) == [40.0, 90.0]


async def test_set_consumption_uses_the_configured_energy_content(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await add_account(
        hass, aioclient_mock, email="one@example.com", tank_id="111111", litres=2000
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator._kwh_per_litre = 9.6

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CONSUMPTION,
        {"liters": 100.0, "entry_id": entry.entry_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.total_consumption_usable_kwh == pytest.approx(960.0)
    assert coordinator.data["total_consumption_usable_kwh"] == pytest.approx(960.0)
