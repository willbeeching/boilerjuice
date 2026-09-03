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
import yarl
from bs4 import BeautifulSoup

from .const import BASE_URL, LOGIN_URL, PRICE_URL, TANKS_URL
from .errors import (
    BoilerJuiceAuthError,
    BoilerJuiceConnectionError,
    BoilerJuiceParseError,
    BoilerJuiceRateLimitError,
    BoilerJuiceServerError,
    RedactedTransportError,
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


def _redact(err: BaseException) -> RedactedTransportError:
    """Return a cause that carries the failure's shape but no identifiers.

    aiohttp's exception text includes the request URL, and both
    ClientResponseError and the connection timeouts do. Interpolating one
    into a message, or chaining it, put the tank id into the log.
    """
    status = getattr(err, "status", None)
    detail = type(err).__name__
    if status is not None:
        detail = f"{detail} (HTTP {status})"
    return RedactedTransportError(detail)


# The only host we will ever send credentials to, or follow a redirect to.
ALLOWED_HOST = yarl.URL(BASE_URL).host

# 307 and 308 preserve the method and the body, so following one blindly
# would re-post the password to wherever the redirect pointed.
BODY_PRESERVING_REDIRECTS = (307, 308)
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# A couple of hops is normal; a chain this long is a loop or a redirector.
MAX_REDIRECTS = 5


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
        """GET `url`, returning (body, final URL).

        Redirects are followed by hand, one validated hop at a time. aiohttp
        follows up to ten by default and validates none of them, so leaving
        it to do so would let any BoilerJuice response point Home Assistant
        at an arbitrary host - including one inside the user's own network.
        The login POST was hardened against exactly this; every other request
        needs the same treatment.
        """
        target = yarl.URL(url)
        for _ in range(MAX_REDIRECTS + 1):
            try:
                async with self.session.get(target, allow_redirects=False) as response:
                    if response.status in REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                    else:
                        self._classify(response.status, description)
                        body = await self._read_text(response, description)
                        return body, str(response.url)
            except aiohttp.ClientError as err:
                raise BoilerJuiceConnectionError(
                    f"Failed to load the {description} ({type(err).__name__})"
                ) from _redact(err)
            except TimeoutError as err:
                raise BoilerJuiceConnectionError(
                    f"Timed out loading the {description}"
                ) from _redact(err)

            target = self._checked_redirect(response.status, location, base=target)

        raise BoilerJuiceConnectionError(
            f"The {description} redirected more than {MAX_REDIRECTS} times"
        )

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
            # Redirects are followed by hand. aiohttp would follow a 307 or a
            # 308 by re-sending the body, so an attacker who could steer the
            # redirect would be handed the password.
            async with self.session.post(
                LOGIN_URL, data=form, allow_redirects=False
            ) as response:
                status = response.status
                location = response.headers.get("Location")
                if status not in REDIRECT_STATUSES:
                    self._classify(status, "BoilerJuice login request")
                    body = await self._read_text(response, "BoilerJuice login response")
                    landed_on_login = str(response.url).rstrip("/") == LOGIN_URL.rstrip(
                        "/"
                    )
                else:
                    body, landed_on_login = "", False
        except aiohttp.ClientError as err:
            raise BoilerJuiceConnectionError(
                f"Login request failed ({type(err).__name__})"
            ) from _redact(err)
        except TimeoutError as err:
            raise BoilerJuiceConnectionError("Login request timed out") from _redact(
                err
            )

        if status in REDIRECT_STATUSES:
            target = self._checked_redirect(status, location)
            # A redirect away from the sign-in page is what success looks
            # like. Follow it with a GET, never by re-posting the credentials.
            body, final_url = await self._async_get(
                str(target), "page after signing in"
            )
            landed_on_login = final_url.rstrip("/") == LOGIN_URL.rstrip("/")

        if landed_on_login and looks_like_login_page(body):
            raise BoilerJuiceAuthError("BoilerJuice rejected the credentials")

        self._signed_in = True

    @staticmethod
    def _checked_redirect(
        status: int, location: str | None, *, base: yarl.URL | str = LOGIN_URL
    ) -> yarl.URL:
        """Return the redirect target, refusing anything off-host.

        A redirect that leaves boilerjuice.com is refused outright rather
        than followed, and a body-preserving redirect is never followed with
        the credentials still attached.
        """
        if not location:
            raise BoilerJuiceConnectionError(
                "BoilerJuice sent a redirect without a destination"
            )

        target = yarl.URL(base).join(yarl.URL(location))
        if (
            target.scheme != "https"
            or target.host != ALLOWED_HOST
            # Credentials smuggled into the URL, or a non-standard port, are
            # not something BoilerJuice has any reason to send us.
            or target.user is not None
            or target.password is not None
            or target.port not in (None, 443)
        ):
            # Deliberately vague: the destination is attacker-controlled in
            # the case this guards against, so it does not go in the log.
            raise BoilerJuiceConnectionError(
                "BoilerJuice redirected somewhere unexpected; refusing to follow it"
            )
        if status in BODY_PRESERVING_REDIRECTS:
            _LOGGER.debug(
                "Following a %s with a GET rather than re-sending the request body",
                status,
            )
        return target

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
