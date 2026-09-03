"""HTTP, sign-in and session lifecycle for one BoilerJuice account.

Knows nothing about consumption, storage or entities: it turns credentials
into validated readings, and every failure into a typed error. The aiohttp
session is injected rather than created here, so Home Assistant owns its
lifecycle and the tests can hand in a mock.

Nothing in this module logs a credential, a tank id, an account name, a CSRF
token or a response body. A scraper's logs end up in bug reports.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import aiohttp
from bs4 import BeautifulSoup

from .const import LOGIN_URL, PRICE_URL, TANKS_URL
from .errors import (
    BoilerJuiceAuthError,
    BoilerJuiceConnectionError,
    BoilerJuiceParseError,
    BoilerJuiceRateLimitError,
    BoilerJuiceServerError,
)
from .models import TankReading
from .parser import (
    looks_like_login_page,
    parse_price,
    parse_tank_ids,
    parse_tank_page,
    validate_tank_id,
)

_LOGGER = logging.getLogger(__name__)

# Every request gets an explicit budget. Without one aiohttp waits forever,
# so a stalled BoilerJuice response would pin the coordinator open until Home
# Assistant restarted.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(
    total=45, connect=10, sock_connect=10, sock_read=20
)

# A tank page is a few hundred kilobytes. Anything vastly larger is not a
# page we can use, and reading it would just burn memory.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

SessionFactory = Callable[[aiohttp.ClientTimeout], aiohttp.ClientSession]


class BoilerJuiceClient:
    """One signed-in conversation with BoilerJuice."""

    def __init__(
        self,
        session_factory: SessionFactory,
        email: str,
        password: str,
    ) -> None:
        """Store the credentials and how to obtain a session."""
        self._session_factory = session_factory
        self._email = email
        self._password = password
        self._session: aiohttp.ClientSession | None = None
        self._signed_in = False

    @property
    def session(self) -> aiohttp.ClientSession:
        """Return the session, creating it on first use."""
        if self._session is None:
            self._session = self._session_factory(REQUEST_TIMEOUT)
        return self._session

    async def async_close(self) -> None:
        """Close the session. Called when the config entry unloads."""
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._signed_in = False

    def invalidate_session(self) -> None:
        """Forget that we are signed in, forcing a fresh sign-in next time."""
        self._signed_in = False

    @staticmethod
    def _classify(status: int, description: str) -> None:
        """Raise the error that matches an HTTP status, if any."""
        if status == 200:
            return
        if status in (401, 403):
            raise BoilerJuiceAuthError(
                f"BoilerJuice refused access to the {description}"
            )
        if status == 429:
            raise BoilerJuiceRateLimitError(
                f"BoilerJuice rate-limited the request for the {description}"
            )
        if status >= 500:
            raise BoilerJuiceServerError(
                f"BoilerJuice returned HTTP {status} for the {description}"
            )
        raise BoilerJuiceConnectionError(
            f"Failed to load the {description} (HTTP {status})"
        )

    @staticmethod
    async def _read_text(response: aiohttp.ClientResponse, description: str) -> str:
        """Read a bounded amount of body text from `response`."""
        raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BoilerJuiceParseError(
                f"The {description} was larger than {MAX_RESPONSE_BYTES} bytes"
            )
        return raw.decode(response.charset or "utf-8", errors="replace")

    async def _async_get(self, url: str, description: str) -> tuple[str, str]:
        """GET `url`, returning (body, final URL)."""
        try:
            async with self.session.get(url) as response:
                self._classify(response.status, description)
                return await self._read_text(response, description), str(response.url)
        except aiohttp.ClientError as err:
            raise BoilerJuiceConnectionError(
                f"Failed to load the {description}: {err}"
            ) from err
        except TimeoutError as err:
            raise BoilerJuiceConnectionError(
                f"Timed out loading the {description}"
            ) from err

    async def _async_sign_in(self) -> None:
        """Drive the BoilerJuice sign-in flow for this session."""
        login_page, _ = await self._async_get(LOGIN_URL, "BoilerJuice login page")

        token = BeautifulSoup(login_page, "html.parser").find(
            "meta", {"name": "csrf-token"}
        )
        if not token or not token.get("content"):
            raise BoilerJuiceConnectionError(
                "Could not find the CSRF token on the BoilerJuice login page"
            )

        form = {
            "user[email]": self._email,
            "user[password]": self._password,
            "authenticity_token": token["content"],
            "commit": "Sign in",
        }

        try:
            async with self.session.post(LOGIN_URL, data=form) as response:
                self._classify(response.status, "BoilerJuice login request")
                body = await self._read_text(response, "BoilerJuice login response")
                # A rejected sign-in re-renders the form, so the reliable
                # signals are the final URL and the presence of the password
                # field, not the words "Sign in" which also appear in the
                # signed-in header.
                landed_on_login = str(response.url).rstrip("/") == LOGIN_URL.rstrip("/")
        except aiohttp.ClientError as err:
            raise BoilerJuiceConnectionError(f"Login request failed: {err}") from err
        except TimeoutError as err:
            raise BoilerJuiceConnectionError("Login request timed out") from err

        if landed_on_login and looks_like_login_page(body):
            raise BoilerJuiceAuthError("BoilerJuice rejected the credentials")

        self._signed_in = True

    async def _async_get_signed_in(self, url: str, description: str) -> str:
        """GET `url` while signed in, renewing the session once if it lapsed.

        BoilerJuice expires sessions on its own schedule, so one silent
        re-sign-in is normal operation rather than an error worth surfacing.
        """
        if not self._signed_in:
            await self._async_sign_in()

        body, _ = await self._async_get(url, description)
        if not looks_like_login_page(body):
            return body

        _LOGGER.debug("The BoilerJuice session lapsed; signing in again")
        self._signed_in = False
        await self._async_sign_in()

        body, _ = await self._async_get(url, description)
        if looks_like_login_page(body):
            raise BoilerJuiceAuthError(
                "BoilerJuice kept returning the sign-in page after a fresh login"
            )
        return body

    async def async_list_tank_ids(self) -> list[str]:
        """Return every tank id on the account, in page order."""
        body = await self._async_get_signed_in(TANKS_URL, "BoilerJuice tanks page")
        return parse_tank_ids(body)

    async def async_fetch_tank(self, tank_id: str | None = None) -> TankReading:
        """Return a validated reading for `tank_id`, or the first tank."""
        if tank_id is None:
            tank_ids = await self.async_list_tank_ids()
            if not tank_ids:
                raise BoilerJuiceParseError(
                    "Could not find a tank on this BoilerJuice account"
                )
            if len(tank_ids) > 1:
                _LOGGER.debug(
                    "Found %d tanks on this account; using the first one",
                    len(tank_ids),
                )
            tank_id = tank_ids[0]

        canonical = validate_tank_id(tank_id)
        if canonical is None:
            raise BoilerJuiceParseError("Refusing to use a non-numeric tank id")

        body = await self._async_get_signed_in(
            f"{TANKS_URL}/{canonical}/edit", "BoilerJuice tank page"
        )
        return parse_tank_page(body, canonical)

    async def async_fetch_price(self) -> float | None:
        """Return the kerosene price in pence per litre, or None.

        The price comes from a separate public page and is strictly optional:
        every failure is swallowed so it can never cost us a tank reading.
        """
        try:
            body, _ = await self._async_get(PRICE_URL, "BoilerJuice price page")
            return parse_price(body)
        except Exception as err:
            _LOGGER.debug("Could not refresh the oil price: %s", err)
            return None
