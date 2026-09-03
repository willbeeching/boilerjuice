"""Entry setup, unload and the service lifecycle."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.boilerjuice import (
    SERVICE_RESET_CONSUMPTION,
    SERVICE_SET_CONSUMPTION,
)
from custom_components.boilerjuice.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

from .helpers import (
    load_fixture,
    make_entry,
    mock_site,
    setup_account,
    tank_page,
)


async def test_setup_registers_the_device_and_services(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION)
    assert hass.services.has_service(DOMAIN, SERVICE_SET_CONSUMPTION)

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "123456")})
    assert device is not None
    assert device.manufacturer == "BoilerJuice"


async def test_unload_closes_the_session_and_removes_the_services(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION)
    assert DOMAIN not in hass.data


async def test_the_services_survive_unloading_one_of_two_accounts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    first = await setup_account(
        hass, aioclient_mock, email="one@example.com", tank_id="111111"
    )
    await setup_account(hass, aioclient_mock, email="two@example.com", tank_id="222222")

    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION)
    assert len(hass.data[DOMAIN]) == 1


async def test_an_unreadable_tank_page_leaves_the_entry_retrying(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_site(aioclient_mock, tank_html=load_fixture("tank_redesigned.html"))
    entry = make_entry(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_rejected_credentials_leave_the_entry_in_setup_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_site(
        aioclient_mock,
        tank_html=tank_page(percentage=80, litres=2000),
        login_html=load_fixture("login.html"),
    )
    entry = make_entry(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_yaml_configuration_starts_an_import_flow(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))

    assert await async_setup_component(
        hass,
        DOMAIN,
        {
            DOMAIN: {
                CONF_EMAIL: "someone@example.com",
                CONF_PASSWORD: "hunter2",
                "tank_id": "123456",
            }
        },
    )
    await hass.async_block_till_done()

    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
