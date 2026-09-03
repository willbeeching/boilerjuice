"""Every way the BoilerJuice round trip can fail, and what it maps onto."""

from __future__ import annotations

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.boilerjuice.const import (
    CONF_KWH_PER_LITRE,
    DEFAULT_KWH_PER_LITRE,
    LOGIN_URL,
    PRICE_URL,
    TANKS_URL,
)
from custom_components.boilerjuice.coordinator import BoilerJuiceDataUpdateCoordinator
from custom_components.boilerjuice.errors import (
    BoilerJuiceAuthError,
    BoilerJuiceConnectionError,
)
from custom_components.boilerjuice.storage import STORAGE_VERSION

from .helpers import (
    PRICE_PAGE,
    SIGNED_IN_PAGE,
    TANK_URL,
    load_fixture,
    make_entry,
    mock_site,
    tank_page,
)


@pytest.fixture
async def coordinator(hass: HomeAssistant) -> BoilerJuiceDataUpdateCoordinator:
    made = BoilerJuiceDataUpdateCoordinator(hass, make_entry(hass))
    yield made
    await made.async_close()


async def test_a_non_200_login_page_is_a_connection_error(
    aioclient_mock: AiohttpClientMocker, coordinator
) -> None:
    aioclient_mock.get(LOGIN_URL, status=502, text="")

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, BoilerJuiceConnectionError)


async def test_a_transport_error_is_a_connection_error(
    aioclient_mock: AiohttpClientMocker, coordinator
) -> None:
    aioclient_mock.get(LOGIN_URL, exc=aiohttp.ClientError("boom"))

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, BoilerJuiceConnectionError)


async def test_a_timeout_is_a_connection_error(
    aioclient_mock: AiohttpClientMocker, coordinator
) -> None:
    aioclient_mock.get(LOGIN_URL, exc=TimeoutError())

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, BoilerJuiceConnectionError)


async def test_a_login_page_without_a_csrf_token_is_a_connection_error(
    aioclient_mock: AiohttpClientMocker, coordinator
) -> None:
    aioclient_mock.get(
        LOGIN_URL, text='<html><body><input name="user[password]"></body></html>'
    )

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, BoilerJuiceConnectionError)


async def test_a_non_200_login_post_is_a_connection_error(
    aioclient_mock: AiohttpClientMocker, coordinator
) -> None:
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, status=500, text="")

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, BoilerJuiceConnectionError)


@pytest.mark.parametrize("failure", [aiohttp.ClientError("boom"), TimeoutError()])
async def test_a_failing_login_post_is_a_connection_error(
    aioclient_mock: AiohttpClientMocker, coordinator, failure: Exception
) -> None:
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, exc=failure)

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, BoilerJuiceConnectionError)


async def test_the_tanks_page_redirecting_to_login_is_an_auth_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = make_entry(hass, tank_id=None)
    made = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
        aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
        aioclient_mock.get(TANKS_URL, text=load_fixture("login.html"))

        await made.async_refresh()

        assert isinstance(made.last_exception, BoilerJuiceAuthError)
    finally:
        await made.async_close()


async def test_the_first_of_several_tanks_is_used(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = make_entry(hass, tank_id=None)
    made = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
        aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
        aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list_multiple.html"))
        aioclient_mock.get(TANK_URL, text=tank_page(percentage=80, litres=2000))
        aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

        await made.async_refresh()
        await hass.async_block_till_done()

        assert made.data["id"] == "123456"
    finally:
        await made.async_close()


async def test_an_unexpected_error_becomes_an_update_failure(
    aioclient_mock: AiohttpClientMocker, coordinator
) -> None:
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANK_URL, exc=ValueError("something unforeseen"))

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, UpdateFailed)


@pytest.mark.parametrize("bad", [0, -1, "not a number", float("nan"), None])
async def test_a_bad_energy_content_falls_back_to_the_default(
    hass: HomeAssistant, bad: object
) -> None:
    entry = make_entry(hass, **{CONF_KWH_PER_LITRE: bad})
    made = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        assert made.kwh_per_litre == DEFAULT_KWH_PER_LITRE
    finally:
        await made.async_close()


async def test_an_unreadable_stored_timestamp_is_refused(
    hass: HomeAssistant, hass_storage
) -> None:
    """A timestamp we cannot parse means the whole document is suspect."""
    entry = make_entry(hass)
    hass_storage[f"boilerjuice.{entry.entry_id}"] = {
        "version": STORAGE_VERSION,
        "data": {"total_litres": 10.0, "last_update": "not-a-timestamp"},
    }
    made = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        await made._async_load()

        assert made.last_level_change is None
        assert made.total_consumption_usable_liters == 0.0
    finally:
        await made.async_close()


@pytest.mark.parametrize(
    "price_html",
    [
        "<html><body>0.00 pence per litre</body></html>",
        "<html><body>99999.00 pence per litre</body></html>",
    ],
)
async def test_an_implausible_price_is_ignored(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, price_html: str
) -> None:
    made = BoilerJuiceDataUpdateCoordinator(hass, make_entry(hass))

    try:
        mock_site(
            aioclient_mock,
            tank_html=tank_page(percentage=80, litres=2000),
            price_html=price_html,
        )
        await made.async_refresh()
        await hass.async_block_till_done()

        assert made.last_update_success
        assert "current_price_pence" not in made.data
    finally:
        await made.async_close()


async def test_consumption_is_derived_from_the_percentage_when_there_is_no_volume(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    made = BoilerJuiceDataUpdateCoordinator(hass, make_entry(hass))

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80))
        await made.async_refresh()
        await hass.async_block_till_done()

        mock_site(aioclient_mock, tank_html=tank_page(percentage=79))
        await made.async_refresh()
        await hass.async_block_till_done()

        # 1% of a 2500 L tank.
        assert made.total_consumption_usable_liters == pytest.approx(25.0)
    finally:
        await made.async_close()


async def test_a_refill_seen_only_in_the_percentage_is_not_consumption(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    made = BoilerJuiceDataUpdateCoordinator(hass, make_entry(hass))

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=20))
        await made.async_refresh()
        await hass.async_block_till_done()

        mock_site(aioclient_mock, tank_html=tank_page(percentage=95))
        await made.async_refresh()
        await hass.async_block_till_done()

        assert made.total_consumption_usable_liters == 0.0
    finally:
        await made.async_close()


async def test_a_reference_cannot_be_set_from_an_empty_reading(coordinator) -> None:
    coordinator._state.reference_volume = 2000
    coordinator._state.reference_level = 80.0

    await coordinator.async_force_consumption_reference({})

    assert coordinator._state.reference_volume == 2000
    assert coordinator._state.reference_level == 80.0
