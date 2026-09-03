"""The client: sign-in, session renewal, response classification, bounds."""

from __future__ import annotations

import aiohttp
import pytest
from custom_components.boilerjuice.client import (
    MAX_RESPONSE_BYTES,
    BoilerJuiceClient,
)
from custom_components.boilerjuice.const import LOGIN_URL, PRICE_URL, TANKS_URL
from custom_components.boilerjuice.errors import (
    BoilerJuiceAuthError,
    BoilerJuiceConnectionError,
    BoilerJuiceParseError,
    BoilerJuiceRateLimitError,
    BoilerJuiceServerError,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import PRICE_PAGE, SIGNED_IN_PAGE, TANK_URL, load_fixture, tank_page


@pytest.fixture
async def client(hass: HomeAssistant) -> BoilerJuiceClient:
    """Return a client backed by a Home Assistant-created session."""
    made = BoilerJuiceClient(
        lambda timeout: async_create_clientsession(
            hass, cookie_jar=aiohttp.CookieJar(), timeout=timeout
        ),
        "someone@example.com",
        "hunter2",
    )
    yield made
    await made.async_close()


def mock_sign_in(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)


async def test_a_signed_in_fetch_returns_a_reading(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    mock_sign_in(aioclient_mock)
    aioclient_mock.get(TANK_URL, text=tank_page(percentage=80, litres=2000))

    reading = await client.async_fetch_tank("123456")

    assert reading.tank_id == "123456"
    assert reading.volume_litres == 2000


async def test_the_session_is_reused_across_fetches(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    """Signing in once per poll is enough; twice would double the load."""
    mock_sign_in(aioclient_mock)
    aioclient_mock.get(TANK_URL, text=tank_page(percentage=80, litres=2000))

    await client.async_fetch_tank("123456")
    await client.async_fetch_tank("123456")

    logins = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert len(logins) == 1


async def test_a_lapsed_session_is_renewed_once_and_the_fetch_retried(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    """BoilerJuice expires sessions on its own schedule; that is not an error."""
    mock_sign_in(aioclient_mock)

    # The tank page comes back as the sign-in page once, then succeeds. The
    # mocker always replays the same response for a URL, so the bounce is
    # staged on the client's own fetch instead.
    bodies = iter([load_fixture("login.html"), tank_page(percentage=80, litres=2000)])
    fetch = client._async_get

    async def bounce_once(url: str, description: str):
        if url == TANK_URL:
            return next(bodies), url
        return await fetch(url, description)

    client._async_get = bounce_once

    reading = await client.async_fetch_tank("123456")

    assert reading.volume_litres == 2000
    logins = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert len(logins) == 2


async def test_a_session_that_will_not_renew_is_an_auth_error(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    mock_sign_in(aioclient_mock)
    aioclient_mock.get(TANK_URL, text=load_fixture("login.html"))

    with pytest.raises(BoilerJuiceAuthError):
        await client.async_fetch_tank("123456")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, BoilerJuiceAuthError),
        (403, BoilerJuiceAuthError),
        (429, BoilerJuiceRateLimitError),
        (500, BoilerJuiceServerError),
        (503, BoilerJuiceServerError),
        (404, BoilerJuiceConnectionError),
    ],
)
async def test_http_statuses_map_onto_distinct_errors(
    aioclient_mock: AiohttpClientMocker,
    client: BoilerJuiceClient,
    status: int,
    expected: type[Exception],
) -> None:
    aioclient_mock.get(LOGIN_URL, status=status, text="")

    with pytest.raises(expected):
        await client.async_fetch_tank("123456")


async def test_an_oversized_response_is_refused(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    """A runaway response must not be read into memory in full."""
    mock_sign_in(aioclient_mock)
    aioclient_mock.get(TANK_URL, text="x" * (MAX_RESPONSE_BYTES + 10))

    with pytest.raises(BoilerJuiceParseError):
        await client.async_fetch_tank("123456")


async def test_a_login_post_that_lands_elsewhere_counts_as_signed_in(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    mock_sign_in(aioclient_mock)
    aioclient_mock.get(TANK_URL, text=tank_page(percentage=80, litres=2000))

    await client.async_fetch_tank("123456")

    assert client._signed_in


async def test_invalidating_the_session_forces_a_fresh_sign_in(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    mock_sign_in(aioclient_mock)
    aioclient_mock.get(TANK_URL, text=tank_page(percentage=80, litres=2000))

    await client.async_fetch_tank("123456")
    client.invalidate_session()
    await client.async_fetch_tank("123456")

    logins = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert len(logins) == 2


async def test_listing_tanks_returns_every_id(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    mock_sign_in(aioclient_mock)
    aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list_multiple.html"))

    assert await client.async_list_tank_ids() == ["123456", "789012"]


async def test_an_account_with_no_tanks_is_a_parse_error(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    mock_sign_in(aioclient_mock)
    aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list_empty.html"))

    with pytest.raises(BoilerJuiceParseError):
        await client.async_fetch_tank()


async def test_a_non_numeric_tank_id_is_a_parse_error(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    mock_sign_in(aioclient_mock)

    with pytest.raises(BoilerJuiceParseError):
        await client.async_fetch_tank("../admin")


async def test_the_price_is_returned_when_the_page_states_one(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    assert await client.async_fetch_price() == 62.45


@pytest.mark.parametrize(
    "failure",
    [
        {"status": 500, "text": ""},
        {"exc": aiohttp.ClientError("boom")},
        {"exc": TimeoutError()},
        {"exc": ValueError("something unforeseen")},
    ],
)
async def test_a_failing_price_request_returns_none_rather_than_raising(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient, failure: dict
) -> None:
    aioclient_mock.get(PRICE_URL, **failure)

    assert await client.async_fetch_price() is None


async def test_a_price_page_without_a_price_returns_none(
    aioclient_mock: AiohttpClientMocker, client: BoilerJuiceClient
) -> None:
    aioclient_mock.get(PRICE_URL, text="<html><body>no price today</body></html>")

    assert await client.async_fetch_price() is None


async def test_closing_the_client_twice_is_harmless(
    client: BoilerJuiceClient,
) -> None:
    await client.async_close()
    await client.async_close()
