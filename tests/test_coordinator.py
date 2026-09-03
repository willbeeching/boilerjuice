"""Consumption must only ever move on a reading we could actually read.

These are the regressions behind the v1.3.2 safety release: before it, any
page the parser did not understand became "0 litres, 0%", and the transition
from the previous good reading to that zero was booked as real consumption.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.boilerjuice.const import (
    CONF_EMAIL,
    CONF_KWH_PER_LITRE,
    CONF_PASSWORD,
    CONF_TANK_ID,
    DOMAIN,
    LOGIN_URL,
    TANKS_URL,
)
from custom_components.boilerjuice.coordinator import (
    BoilerJuiceAuthError,
    BoilerJuiceDataUpdateCoordinator,
)

from .helpers import (
    SIGNED_IN_PAGE,
    TANK_ID,
    load_fixture,
    make_entry,
    mock_site,
    tank_page,
)


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a config entry pinned to a known tank."""
    return make_entry(hass)


@pytest.fixture
async def coordinator(
    hass: HomeAssistant, entry: MockConfigEntry
) -> BoilerJuiceDataUpdateCoordinator:
    """Return a coordinator for that entry."""
    made = BoilerJuiceDataUpdateCoordinator(hass, entry)
    yield made
    await made.async_close()


async def settle(hass: HomeAssistant) -> None:
    """Let the background storage write finish."""
    await hass.async_block_till_done()


async def test_first_poll_sets_a_baseline_without_booking_consumption(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    coordinator: BoilerJuiceDataUpdateCoordinator,
) -> None:
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))

    await coordinator.async_refresh()
    await settle(hass)

    assert coordinator.last_update_success
    assert coordinator.total_consumption_usable_liters == 0.0
    assert coordinator.data["usable_volume_litres"] == 2000
    assert coordinator.data["total_level_percentage"] == 80


async def test_ordinary_consumption_is_recorded_from_the_volume_drop(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    coordinator: BoilerJuiceDataUpdateCoordinator,
) -> None:
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
    await coordinator.async_refresh()
    await settle(hass)

    mock_site(aioclient_mock, tank_html=tank_page(percentage=79, litres=1975))
    await coordinator.async_refresh()
    await settle(hass)

    assert coordinator.total_consumption_usable_liters == 25.0


async def test_a_refill_is_not_recorded_as_consumption(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    coordinator: BoilerJuiceDataUpdateCoordinator,
) -> None:
    mock_site(aioclient_mock, tank_html=tank_page(percentage=20, litres=500))
    await coordinator.async_refresh()
    await settle(hass)

    mock_site(aioclient_mock, tank_html=tank_page(percentage=95, litres=2375))
    await coordinator.async_refresh()
    await settle(hass)

    assert coordinator.total_consumption_usable_liters == 0.0


async def test_an_unchanged_reading_records_nothing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    coordinator: BoilerJuiceDataUpdateCoordinator,
) -> None:
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
    await coordinator.async_refresh()
    await settle(hass)
    await coordinator.async_refresh()
    await settle(hass)

    assert coordinator.total_consumption_usable_liters == 0.0


@pytest.mark.parametrize(
    "broken_html",
    [
        pytest.param("", id="empty"),
        pytest.param("<html><body></body></html>", id="blank-page"),
        pytest.param("<html><body><div id='usable-oil'", id="truncated"),
        pytest.param(
            '<html><body><main data-testid="oil">62%</main></body></html>',
            id="redesigned",
        ),
    ],
)
async def test_an_unreadable_page_cannot_move_consumption(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    coordinator: BoilerJuiceDataUpdateCoordinator,
    broken_html: str,
) -> None:
    """The whole point of the release: no reading, no change."""
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
    await coordinator.async_refresh()
    await settle(hass)
    good_data = dict(coordinator.data)

    mock_site(aioclient_mock, tank_html=broken_html)
    await coordinator.async_refresh()
    await settle(hass)

    assert not coordinator.last_update_success
    assert coordinator.total_consumption_usable_liters == 0.0
    assert coordinator.total_consumption_usable_kwh == 0.0
    assert coordinator.daily_consumption_usable_liters == 0.0
    # The last good reading is still what consumers see.
    assert coordinator.data == good_data


async def test_a_session_that_expired_mid_poll_is_an_auth_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    coordinator: BoilerJuiceDataUpdateCoordinator,
) -> None:
    mock_site(aioclient_mock, tank_html=load_fixture("login.html"))

    await coordinator.async_refresh()
    await settle(hass)

    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception, BoilerJuiceAuthError)
    assert coordinator.total_consumption_usable_liters == 0.0


async def test_rejected_credentials_are_an_auth_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    coordinator: BoilerJuiceDataUpdateCoordinator,
) -> None:
    mock_site(
        aioclient_mock,
        tank_html=tank_page(percentage=80, litres=2000),
        login_html=load_fixture("login.html"),
    )

    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception, BoilerJuiceAuthError)


async def test_a_page_with_a_level_but_no_volume_keeps_the_volume_reference(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    coordinator: BoilerJuiceDataUpdateCoordinator,
) -> None:
    """A partial page must not blank the volume baseline.

    Zeroing it made the following full page look like a 2000 L refill, and
    the page after that like 2000 L of consumption.
    """
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
    await coordinator.async_refresh()
    await settle(hass)

    mock_site(aioclient_mock, tank_html=tank_page(percentage=80))
    await coordinator.async_refresh()
    await settle(hass)

    assert coordinator.last_update_success
    assert coordinator.total_consumption_usable_liters == 0.0

    mock_site(aioclient_mock, tank_html=tank_page(percentage=79, litres=1980))
    await coordinator.async_refresh()
    await settle(hass)

    assert coordinator.total_consumption_usable_liters == 20.0


async def test_kwh_follows_the_configured_energy_content(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """kWh is derived from litres with the configured factor, not 10.35."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "someone@example.com",
            CONF_PASSWORD: "hunter2",
            CONF_TANK_ID: TANK_ID,
            CONF_KWH_PER_LITRE: 9.6,
        },
        unique_id="someone@example.com",
    )
    config_entry.add_to_hass(hass)
    made = BoilerJuiceDataUpdateCoordinator(hass, config_entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await made.async_refresh()
        await settle(hass)

        mock_site(aioclient_mock, tank_html=tank_page(percentage=79, litres=1900))
        await made.async_refresh()
        await settle(hass)

        assert made.total_consumption_usable_liters == 100.0
        assert made.total_consumption_usable_kwh == pytest.approx(960.0)
        assert made.data["total_consumption_usable_kwh"] == pytest.approx(960.0)
        assert made.data["kwh_per_litre"] == 9.6
    finally:
        await made.async_close()


async def test_a_failed_price_request_keeps_the_last_good_price(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    coordinator: BoilerJuiceDataUpdateCoordinator,
) -> None:
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
    await coordinator.async_refresh()
    await settle(hass)
    assert coordinator.data["current_price_pence"] == 62.45

    mock_site(
        aioclient_mock, tank_html=tank_page(percentage=80, litres=2000), price_html=None
    )
    await coordinator.async_refresh()
    await settle(hass)

    assert coordinator.last_update_success
    assert coordinator.data["current_price_pence"] == 62.45
    assert "price_last_updated" in coordinator.data


async def test_an_account_with_no_tanks_fails_the_update(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        unique_id="someone@example.com",
    )
    config_entry.add_to_hass(hass)
    made = BoilerJuiceDataUpdateCoordinator(hass, config_entry)

    try:
        aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
        aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
        aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list_empty.html"))

        await made.async_refresh()

        assert not made.last_update_success
    finally:
        await made.async_close()


async def test_a_non_numeric_configured_tank_id_is_ignored(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A bad tank id falls back to discovery rather than building a bad URL."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "someone@example.com",
            CONF_PASSWORD: "hunter2",
            CONF_TANK_ID: "../admin",
        },
        unique_id="someone@example.com",
    )
    config_entry.add_to_hass(hass)
    made = BoilerJuiceDataUpdateCoordinator(hass, config_entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await made.async_refresh()
        await settle(hass)

        assert made.last_update_success
        assert made.data["id"] == TANK_ID
    finally:
        await made.async_close()
