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
from custom_components.boilerjuice.const import DOMAIN

from .helpers import setup_account


@pytest.fixture
async def two_accounts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> tuple[MockConfigEntry, MockConfigEntry]:
    """Two configured accounts, each with recorded consumption."""
    first = await setup_account(
        hass, aioclient_mock, email="one@example.com", tank_id="111111", litres=2000
    )
    second = await setup_account(
        hass, aioclient_mock, email="two@example.com", tank_id="222222", litres=1500
    )

    for entry, total in ((first, 40.0), (second, 90.0)):
        hass.data[DOMAIN][entry.entry_id]._state.total_litres = total

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
    entry = await setup_account(
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


async def test_a_label_target_reaches_only_that_account(
    hass: HomeAssistant, two_accounts
) -> None:
    from homeassistant.helpers import label_registry as lr

    first, second = two_accounts
    label = lr.async_get(hass).async_create("Oil")
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, "111111")})
    device_registry.async_update_device(device.id, labels={label.label_id})

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"label_id": label.label_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, first, second) == [0.0, 90.0]


async def test_an_area_target_reaches_only_that_account(
    hass: HomeAssistant, two_accounts
) -> None:
    from homeassistant.helpers import area_registry as ar

    first, second = two_accounts
    area = ar.async_get(hass).async_create("Utility")
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, "222222")})
    device_registry.async_update_device(device.id, area_id=area.id)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"area_id": area.id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, first, second) == [40.0, 0.0]


async def test_a_list_of_entry_ids_is_accepted(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"entry_id": [first.entry_id, second.entry_id]},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, first, second) == [0.0, 0.0]


async def test_an_unknown_entry_id_is_refused(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"entry_id": "not-a-real-entry"},
            blocking=True,
        )

    assert totals(hass, first, second) == [40.0, 90.0]


async def test_an_unknown_device_id_is_refused(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"device_id": "not-a-real-device"},
            blocking=True,
        )

    assert totals(hass, first, second) == [40.0, 90.0]


async def test_a_single_account_needs_no_target(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)
    hass.data[DOMAIN][entry.entry_id]._state.total_litres = 40.0

    await hass.services.async_call(DOMAIN, SERVICE_RESET_CONSUMPTION, {}, blocking=True)
    await hass.async_block_till_done()

    assert totals(hass, entry) == [0.0]


async def test_calling_a_service_with_nothing_loaded_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await setup_account(hass, aioclient_mock)
    # Keep the services registered while emptying the coordinator registry,
    # which is what a call racing an unload would see.
    hass.data[DOMAIN] = {}

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_RESET_CONSUMPTION, {}, blocking=True
        )


async def test_set_consumption_also_sets_the_daily_rate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CONSUMPTION,
        {"liters": 100.0, "daily": 7.5, "entry_id": entry.entry_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.daily_consumption_usable_liters == 7.5
    assert coordinator.data["daily_consumption_usable_liters"] == 7.5


async def test_set_consumption_skips_an_account_with_no_reading_yet(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.data = None

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CONSUMPTION,
        {"liters": 100.0, "entry_id": entry.entry_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.total_consumption_usable_liters == 0.0
