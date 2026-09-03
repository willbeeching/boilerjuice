"""The config flow must not create an entry it could not actually validate."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries, data_entry_flow
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
    PRICE_URL,
    TANKS_URL,
)

from .helpers import (
    PRICE_PAGE,
    SIGNED_IN_PAGE,
    TANK_ID,
    TANK_URL,
    load_fixture,
    tank_page,
)

USER_INPUT = {
    CONF_EMAIL: "someone@example.com",
    CONF_PASSWORD: "hunter2",
    CONF_TANK_ID: TANK_ID,
    CONF_KWH_PER_LITRE: 10.35,
}


def mock_successful_site(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list.html"))
    aioclient_mock.get(TANK_URL, text=load_fixture("tank_current.html"))
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)


async def start_flow(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_the_form_is_shown_first(hass: HomeAssistant) -> None:
    result = await start_flow(hass)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_a_valid_account_creates_an_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_successful_site(aioclient_mock)
    result = await start_flow(hass)

    with patch(
        "custom_components.boilerjuice.async_setup_entry", return_value=True
    ) as setup_entry:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "H2500T"
    assert result["data"][CONF_EMAIL] == "someone@example.com"
    assert len(setup_entry.mock_calls) == 1


async def test_rejected_credentials_show_invalid_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=load_fixture("login.html"))
    result = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_an_unreachable_site_shows_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(LOGIN_URL, status=502, text="")
    result = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_an_unreadable_tank_page_shows_the_unknown_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A parse failure is neither bad credentials nor an unreachable site."""
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list.html"))
    aioclient_mock.get(TANK_URL, text=load_fixture("tank_redesigned.html"))
    result = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["errors"] == {"base": "unknown"}


@pytest.mark.parametrize("bad_tank_id", ["abc", "../admin", "12 34", "1e5"])
async def test_a_non_numeric_tank_id_is_rejected_before_any_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, bad_tank_id: str
) -> None:
    result = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_TANK_ID: bad_tank_id}
    )

    assert result["errors"] == {CONF_TANK_ID: "invalid_tank_id"}
    assert not aioclient_mock.mock_calls


async def test_the_same_account_cannot_be_added_twice(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    MockConfigEntry(
        domain=DOMAIN, data=USER_INPUT, unique_id="someone@example.com"
    ).add_to_hass(hass)
    mock_successful_site(aioclient_mock)
    result = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_yaml_import_goes_through_the_same_validation(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_successful_site(aioclient_mock)

    with patch("custom_components.boilerjuice.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY


async def test_the_tank_name_is_used_when_there_is_no_model(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list.html"))
    aioclient_mock.get(
        TANK_URL,
        text=tank_page(percentage=80, litres=2000)
        + '<input id="tank_user_tanks_attributes_0_name" value="Barn Tank">',
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)
    result = await start_flow(hass)

    with patch("custom_components.boilerjuice.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["title"] == "Barn Tank"


async def test_a_tank_with_neither_model_nor_name_gets_the_default_title(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list.html"))
    aioclient_mock.get(TANK_URL, text=tank_page(percentage=80, litres=2000))
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)
    result = await start_flow(hass)

    with patch("custom_components.boilerjuice.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["title"] == "BoilerJuice Tank"
