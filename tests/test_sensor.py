"""Every sensor's state, and what it does when a value is missing."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import (
    TANK_ID,
    coordinator_of,
    load_fixture,
    mock_site,
    reading_of,
    setup_account,
    tank_page,
    tracker_of,
)


async def test_the_full_set_of_sensors_is_created(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_site(aioclient_mock, tank_html=load_fixture("tank_current.html"))
    await setup_account(hass, aioclient_mock)

    states = [
        state
        for state in hass.states.async_all("sensor")
        if state.entity_id.startswith("sensor.")
    ]
    assert len(states) == 14


async def test_sensor_values_come_from_the_parsed_page(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_site(aioclient_mock, tank_html=load_fixture("tank_current.html"))
    await setup_account(hass, aioclient_mock)

    def value(suffix: str) -> str:
        matches = [
            state
            for state in hass.states.async_all("sensor")
            if state.entity_id.endswith(suffix)
        ]
        assert len(matches) == 1, f"expected one {suffix}, got {matches}"
        return matches[0].state

    assert value("_oil_level") == "62.5"
    assert value("_volume") == "1562"
    assert value("_capacity") == "2500"
    assert value("_height") == "120"
    assert value("_oil_energy_content") == "10.35"
    assert value("_oil_price") == "0.62"
    assert value("_oil_cost_per_kwh") == "0.0603"
    assert value("_total_oil_consumption") == "0.0"


async def test_the_price_sensor_is_unknown_before_a_price_is_seen(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_site(
        aioclient_mock,
        tank_html=tank_page(percentage=80, litres=2000),
        price_html="<html><body>no price today</body></html>",
    )
    await setup_account(hass, aioclient_mock)

    price = next(
        state
        for state in hass.states.async_all("sensor")
        if state.entity_id.endswith("_oil_price")
    )
    assert price.state == "unknown"


async def test_sensors_go_unavailable_when_the_page_stops_parsing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)

    mock_site(aioclient_mock, tank_html=load_fixture("tank_redesigned.html"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    level = next(
        state
        for state in hass.states.async_all("sensor")
        if state.entity_id.endswith("_oil_level")
    )
    assert level.state == "unavailable"


async def test_the_seasonal_sensor_exposes_its_breakdown(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)

    mock_site(aioclient_mock, tank_html=tank_page(percentage=70, litres=1750))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    seasonal = next(
        state
        for state in hass.states.async_all("sensor")
        if state.entity_id.endswith("_seasonal_oil_consumption")
    )
    assert "current_season" in seasonal.attributes
    assert seasonal.attributes["current_season"] in {
        "winter",
        "spring",
        "summer",
        "autumn",
    }


async def test_the_last_update_sensor_reports_when_the_level_last_moved(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)

    last_update = next(
        state
        for state in hass.states.async_all("sensor")
        if state.entity_id.endswith("_last_level_change")
    )
    assert last_update.state != "unknown"
    assert tracker_of(coordinator).last_level_change is not None


async def test_every_sensor_reports_unknown_when_there_is_no_reading(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A tank with no reading must not make any sensor raise."""
    from custom_components.boilerjuice.sensor import SENSORS, BoilerJuiceSensor

    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)

    entities = [
        BoilerJuiceSensor(coordinator, TANK_ID, description) for description in SENSORS
    ]
    assert len(entities) == 14

    coordinator.data = None

    for entity in entities:
        entity.hass = hass
        assert entity.native_value is None
        assert entity.extra_state_attributes is None
        assert not entity.available


async def test_every_sensor_has_a_translated_name() -> None:
    """A missing translation shows up as a nameless entity in the UI."""
    import json
    import pathlib as _pathlib

    from custom_components.boilerjuice.sensor import SENSORS

    for name in ("strings.json", "translations/en.json"):
        translations = json.loads(
            (_pathlib.Path("custom_components/boilerjuice") / name).read_text(
                encoding="utf-8"
            )
        )
        named = translations["entity"]["sensor"]
        for description in SENSORS:
            assert description.translation_key in named, (name, description.key)


async def test_asking_a_sensor_to_update_refreshes_the_coordinator(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    from custom_components.boilerjuice.sensor import SENSORS, BoilerJuiceSensor

    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)
    entity = BoilerJuiceSensor(coordinator, TANK_ID, SENSORS[0])
    entity.hass = hass

    mock_site(aioclient_mock, tank_html=tank_page(percentage=60, litres=1500))
    await entity.async_update()
    await hass.async_block_till_done()

    assert reading_of(coordinator)["total_level_percentage"] == 60
