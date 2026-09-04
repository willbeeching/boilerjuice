"""Adding, repairing and reconfiguring a BoilerJuice account."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from custom_components.boilerjuice.const import (
    CONF_KWH_PER_LITRE,
    CONF_TANK_ID,
    CONF_TANKS,
    DOMAIN,
    LOGIN_URL,
    PRICE_URL,
    TANKS_URL,
)
from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import (
    PRICE_PAGE,
    SIGNED_IN_PAGE,
    TANK_URL,
    load_fixture,
    mock_site,
    setup_account,
    tank_page,
)

USER_INPUT = {
    CONF_EMAIL: "Someone@Example.com",
    CONF_PASSWORD: "hunter2",
    CONF_KWH_PER_LITRE: 10.35,
}


def mock_account(
    aioclient_mock: AiohttpClientMocker,
    tanks: str = "tanks_list.html",
    *,
    clear: bool = True,
):
    """Register a whole working account.

    The mocker replays the first match, so an existing registration has to be
    cleared before a different response can be staged.
    """
    if clear:
        aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=load_fixture(tanks))
    aioclient_mock.get(TANK_URL, text=tank_page(percentage=80, litres=2000))
    aioclient_mock.get(
        f"{TANKS_URL}/789012/edit", text=tank_page(percentage=40, litres=900)
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)


async def start_flow(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


# --- adding an account ----------------------------------------------------


async def test_the_form_is_shown_first(hass: HomeAssistant) -> None:
    result = await start_flow(hass)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_a_valid_account_creates_an_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_account(aioclient_mock)
    result = await start_flow(hass)

    with patch(
        "custom_components.boilerjuice.async_setup_entry", return_value=True
    ) as setup_entry:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    # The entry is the account, so it is titled and identified by the email.
    assert result["title"] == "someone@example.com"
    assert result["result"].unique_id == "someone@example.com"
    assert len(setup_entry.mock_calls) == 1


async def test_the_same_account_cannot_be_added_twice_in_different_case(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    MockConfigEntry(
        domain=DOMAIN, data=USER_INPUT, unique_id="someone@example.com"
    ).add_to_hass(hass)
    mock_account(aioclient_mock)
    result = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_rejected_credentials_show_invalid_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=load_fixture("login.html"))
    result = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

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


async def test_an_account_with_no_tanks_says_so(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_account(aioclient_mock, tanks="tanks_list_empty.html")
    result = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["errors"] == {"base": "no_tanks"}


async def test_an_unexpected_failure_shows_the_unknown_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_account(aioclient_mock)
    result = await start_flow(hass)

    with patch(
        "custom_components.boilerjuice.config_flow.async_validate_account",
        side_effect=ValueError("something unforeseen"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["errors"] == {"base": "unknown"}


# --- YAML import ----------------------------------------------------------


async def test_yaml_import_creates_an_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_account(aioclient_mock)

    with patch("custom_components.boilerjuice.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY


async def test_yaml_import_carries_a_pinned_tank_across(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An existing single-tank install keeps tracking exactly that tank."""
    mock_account(aioclient_mock)

    with patch("custom_components.boilerjuice.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data={
                CONF_EMAIL: "someone@example.com",
                CONF_PASSWORD: "hunter2",
                CONF_TANK_ID: "123456",
            },
        )
        await hass.async_block_till_done()

    assert result["data"][CONF_TANK_ID] == "123456"


@pytest.mark.parametrize("bad_tank_id", ["abc", "../admin", "12 34", "1e5"])
async def test_yaml_import_drops_a_non_numeric_tank_id(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, bad_tank_id: str
) -> None:
    mock_account(aioclient_mock)

    with patch("custom_components.boilerjuice.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data={
                CONF_EMAIL: "someone@example.com",
                CONF_PASSWORD: "hunter2",
                CONF_TANK_ID: bad_tank_id,
            },
        )
        await hass.async_block_till_done()

    assert CONF_TANK_ID not in result["data"]


# --- reauthentication -----------------------------------------------------


async def test_expired_credentials_start_a_reauth_flow(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A password change at BoilerJuice must ask, not retry for ever."""
    entry = await setup_account(hass, aioclient_mock)

    mock_site(
        aioclient_mock,
        tank_html=tank_page(percentage=80, litres=2000),
        login_html=load_fixture("login.html"),
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN
    ]
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_reauth_accepts_a_new_password(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mock_account(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "a-new-password"}
    )
    await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "a-new-password"


async def test_reauth_rejects_a_password_that_still_does_not_work(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)

    result = await entry.start_reauth_flow(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=load_fixture("login.html"))

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "still-wrong"}
    )

    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.parametrize("bad", [0, -1, 0.05, 101, "not a number"])
async def test_the_form_refuses_an_impossible_energy_content(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, bad: object
) -> None:
    """The coordinator falls back to the default; the form should say no first."""
    mock_account(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with pytest.raises(data_entry_flow.InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_KWH_PER_LITRE: bad}
        )


async def test_reconfigure_refuses_an_impossible_energy_content(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock, tank_id=None)
    mock_account(aioclient_mock)

    result = await entry.start_reconfigure_flow(hass)
    with pytest.raises(data_entry_flow.InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_KWH_PER_LITRE: 0, CONF_TANKS: []}
        )

    assert CONF_KWH_PER_LITRE not in entry.options


@pytest.mark.parametrize("flow", ["reauth", "reconfigure"])
async def test_a_successful_flow_reloads_the_entry_exactly_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, flow: str
) -> None:
    """One reload, from async_update_reload_and_abort and nowhere else.

    The integration used to register an update listener as well, so every
    reauthentication and reconfiguration reloaded the entry twice. Home
    Assistant 2026.9 logs that combination and 2026.12 rejects it.
    """
    entry = await setup_account(hass, aioclient_mock, tank_id=None)
    mock_account(aioclient_mock)

    if flow == "reauth":
        result = await entry.start_reauth_flow(hass)
        # A password that really differs: an unchanged entry never woke the
        # listener, so it hid the second reload.
        user_input: dict[str, object] = {CONF_PASSWORD: "a-different-password"}
    else:
        result = await entry.start_reconfigure_flow(hass)
        user_input = {CONF_KWH_PER_LITRE: 10.35, CONF_TANKS: []}

    with patch(
        "homeassistant.config_entries.ConfigEntries.async_reload",
        wraps=hass.config_entries.async_reload,
    ) as reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert reload.call_count == 1


async def test_no_update_listener_is_registered(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The reload belongs to the config-flow helpers, not to a listener."""
    entry = await setup_account(hass, aioclient_mock, tank_id=None)

    assert entry.update_listeners == []


# --- reconfiguration ------------------------------------------------------


async def test_reconfigure_can_narrow_the_tracked_tanks(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock, tank_id=None)
    mock_account(aioclient_mock, tanks="tanks_list_multiple.html")

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_KWH_PER_LITRE: 9.6, CONF_TANKS: ["789012"]},
    )
    await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.options[CONF_TANKS] == ["789012"]
    assert entry.options[CONF_KWH_PER_LITRE] == 9.6


async def test_reconfigure_can_change_the_password(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock, tank_id=None)
    mock_account(aioclient_mock)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: "a-new-password", CONF_KWH_PER_LITRE: 10.35, CONF_TANKS: []},
    )
    await hass.async_block_till_done()

    assert entry.data[CONF_PASSWORD] == "a-new-password"


async def test_reconfigure_rejects_credentials_that_do_not_work(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock, tank_id=None)

    result = await entry.start_reconfigure_flow(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=load_fixture("login.html"))

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: "wrong", CONF_KWH_PER_LITRE: 10.35, CONF_TANKS: []},
    )

    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.parametrize(
    ("tanks_fixture", "expected"),
    [
        ("tanks_list_empty.html", "no_tanks"),
    ],
)
async def test_reauth_reports_an_account_with_no_tanks(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    tanks_fixture: str,
    expected: str,
) -> None:
    entry = await setup_account(hass, aioclient_mock)

    result = await entry.start_reauth_flow(hass)
    mock_account(aioclient_mock, tanks=tanks_fixture)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "hunter2"}
    )

    assert result["errors"] == {"base": expected}


async def test_reauth_reports_an_unreachable_site(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)

    result = await entry.start_reauth_flow(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, status=502, text="")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "hunter2"}
    )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_reports_an_account_with_no_tanks(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock, tank_id=None)

    result = await entry.start_reconfigure_flow(hass)
    mock_account(aioclient_mock, tanks="tanks_list_empty.html")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_KWH_PER_LITRE: 10.35, CONF_TANKS: []}
    )

    assert result["errors"] == {"base": "no_tanks"}


async def test_reconfigure_reports_an_unreachable_site(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock, tank_id=None)

    result = await entry.start_reconfigure_flow(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, status=502, text="")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_KWH_PER_LITRE: 10.35, CONF_TANKS: []}
    )

    assert result["errors"] == {"base": "cannot_connect"}
