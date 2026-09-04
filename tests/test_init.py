"""Entry setup, unload and the service lifecycle."""

from __future__ import annotations

import pytest
from custom_components.boilerjuice import (
    SERVICE_RESET_CONSUMPTION,
    SERVICE_SET_CONSUMPTION,
)
from custom_components.boilerjuice.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import (
    load_fixture,
    make_entry,
    mock_site,
    setup_account,
    tank_device,
    tank_page,
)


async def test_setup_registers_the_device_and_services(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION)
    assert hass.services.has_service(DOMAIN, SERVICE_SET_CONSUMPTION)

    device = tank_device(hass, entry, "123456")
    assert device is not None
    assert device.manufacturer == "BoilerJuice"


async def test_unloading_leaves_the_actions_registered(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The actions belong to the integration, not to an entry.

    Removing them on the last unload turned a call into "unknown service",
    which tells an automation author nothing. They stay, and answer with a
    translated "no BoilerJuice accounts are currently loaded".
    """
    entry = await setup_account(hass, aioclient_mock)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION)

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            DOMAIN, SERVICE_RESET_CONSUMPTION, {}, blocking=True
        )
    assert raised.value.translation_key == "no_accounts_loaded"


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
    assert len(hass.config_entries.async_loaded_entries(DOMAIN)) == 1


async def test_an_unreadable_tank_page_leaves_the_entry_retrying(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_site(aioclient_mock, tank_html=load_fixture("tank_redesigned.html"))
    entry = make_entry(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_rejected_credentials_ask_for_reauthentication(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Bad credentials must ask the user, not retry hourly for ever."""
    mock_site(
        aioclient_mock,
        tank_html=tank_page(percentage=80, litres=2000),
        login_html=load_fixture("login.html"),
    )
    entry = make_entry(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert [
        flow["context"]["source"]
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN
    ] == ["reauth"]


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


async def test_removing_an_entry_deletes_its_stored_history(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    entry = await setup_account(hass, aioclient_mock)
    key = f"{DOMAIN}.{entry.entry_id}"
    assert key in hass_storage

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_version_1_entry_is_migrated_without_touching_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Upgrading must not rename an entity or duplicate a device."""
    from custom_components.boilerjuice.const import CONF_TANK_ID
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            CONF_EMAIL: "Someone@Example.com",
            CONF_PASSWORD: "hunter2",
            CONF_TANK_ID: "123456",
        },
        unique_id="Someone@Example.com",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    # The email is normalised so the same account cannot be added twice.
    assert entry.unique_id == "someone@example.com"
    # Entities and devices are keyed by tank id, which has not changed.
    assert tank_device(hass, entry, "123456")
    assert any(
        state.entity_id.endswith("_oil_level")
        for state in hass.states.async_all("sensor")
    )


async def test_the_deprecated_yaml_block_warns(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))

    assert await async_setup_component(
        hass,
        DOMAIN,
        {DOMAIN: {CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"}},
    )
    await hass.async_block_till_done()

    assert "deprecated" in caplog.text
