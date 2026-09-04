"""Errors raised while talking to BoilerJuice.

All of these subclass UpdateFailed so the coordinator logs one warning and
retries on the next interval, but they are distinct types so callers (the
config flow, the reauth flow, the tests) can tell a wrong password from a
site outage from a page we no longer understand.
"""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import UpdateFailed


class BoilerJuiceError(UpdateFailed):
    """Base class for every BoilerJuice failure."""


class BoilerJuiceAuthError(BoilerJuiceError):
    """BoilerJuice rejected the credentials, or the session expired."""


class BoilerJuiceConnectionError(BoilerJuiceError):
    """BoilerJuice could not be reached or the login flow could not be driven."""


class BoilerJuiceRateLimitError(BoilerJuiceConnectionError):
    """BoilerJuice asked us to slow down."""


class BoilerJuiceServerError(BoilerJuiceConnectionError):
    """BoilerJuice returned a server error."""


class RedactedTransportError(Exception):
    """A transport failure with its details stripped out.

    Stands in as the `__cause__` of a connection error. aiohttp puts the
    request URL in its own exception text, and a tank page URL contains the
    tank id, so chaining the original would put that id into any traceback -
    including the one Home Assistant's own coordinator logs at debug level.
    This keeps the shape of the failure without the identifier.
    """


class BoilerJuiceParseError(BoilerJuiceError):
    """A BoilerJuice page did not yield a usable tank reading.

    Raised instead of returning a partially-filled reading. A truncated page,
    a login redirect or a site redesign all land here, and the coordinator
    keeps its previous state rather than recording a phantom drop to zero.
    """
