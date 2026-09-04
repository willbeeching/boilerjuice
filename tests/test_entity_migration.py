"""Upgrading must keep every existing entity, under its existing entity id."""

from __future__ import annotations

import pytest
from custom_components.boilerjuice.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TANK_ID,
    DOMAIN,
)
from custom_components.boilerjuice.sensor import SENSORS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import TANK_ID, mock_site, tank_page

# What v1.3.1 wrote into the entity registry.
LEGACY_IDS = {
    "BoilerJuiceOilLevelSensor": "sensor.garden_tank_oil_level",
    "BoilerJuiceTotalConsumptionKwhSensor": "sensor.garden_tank_total_oil_consumption_kwh",
    "BoilerJuiceLastUpdateSensor": "sensor.garden_tank_last_updated",
    "BoilerJuiceIncrementalConsumptionKwhSensor": "sensor.garden_tank_oil_consumption_kwh",
}


@pytest.fixture
def upgraded_from_v1(hass: HomeAssistant) -> MockConfigEntry:
    """Return an entry and registry as an existing v1.3.1 install has them."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            CONF_EMAIL: "someone@example.com",
            CONF_PASSWORD: "hunter2",
            CONF_TANK_ID: TANK_ID,
        },
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    for legacy_class, entity_id in LEGACY_IDS.items():
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{TANK_ID}_{legacy_class}",
            suggested_object_id=entity_id.removeprefix("sensor."),
            config_entry=entry,
        )
    return entry


async def test_existing_entities_keep_their_entity_ids(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upgraded_from_v1: MockConfigEntry,
) -> None:
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))

    assert await hass.config_entries.async_setup(upgraded_from_v1.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for legacy_class, entity_id in LEGACY_IDS.items():
        if legacy_class == "BoilerJuiceIncrementalConsumptionKwhSensor":
            continue
        assert registry.async_get(entity_id) is not None, entity_id


async def test_unique_ids_move_onto_stable_keys(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upgraded_from_v1: MockConfigEntry,
) -> None:
    """Class names were the old unique ids, so a rename renamed an entity."""
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))

    assert await hass.config_entries.async_setup(upgraded_from_v1.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert (
        registry.async_get("sensor.garden_tank_oil_level").unique_id
        == f"{TANK_ID}_oil_level"
    )
    assert (
        registry.async_get("sensor.garden_tank_last_updated").unique_id
        == f"{TANK_ID}_last_level_change"
    )


async def test_the_retired_incremental_sensor_is_removed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upgraded_from_v1: MockConfigEntry,
) -> None:
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))

    assert await hass.config_entries.async_setup(upgraded_from_v1.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get("sensor.garden_tank_oil_consumption_kwh") is None


async def test_a_second_setup_does_not_migrate_twice(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upgraded_from_v1: MockConfigEntry,
) -> None:
    """A stale legacy row must not collide with the migrated one."""
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
    assert await hass.config_entries.async_setup(upgraded_from_v1.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    # Recreate a stale legacy row, as a half-finished upgrade might leave.
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{TANK_ID}_BoilerJuiceOilLevelSensor",
        config_entry=upgraded_from_v1,
    )

    await hass.config_entries.async_reload(upgraded_from_v1.entry_id)
    await hass.async_block_till_done()

    assert (
        registry.async_get("sensor.garden_tank_oil_level").unique_id
        == f"{TANK_ID}_oil_level"
    )


async def test_every_sensor_carries_a_legacy_class_except_the_new_one() -> None:
    """A missing mapping would silently orphan somebody's entity."""
    without_legacy = [
        description.key for description in SENSORS if description.legacy_class is None
    ]

    assert without_legacy == ["last_successful_update", "heating_season"]
