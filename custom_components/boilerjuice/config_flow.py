"""Config flow for BoilerJuice."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_KWH_PER_LITRE, CONF_TANK_ID, DEFAULT_KWH_PER_LITRE, DOMAIN
from .coordinator import BoilerJuiceDataUpdateCoordinator
from .errors import BoilerJuiceAuthError, BoilerJuiceConnectionError
from .parser import validate_tank_id

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_TANK_ID): str,
        vol.Optional(CONF_KWH_PER_LITRE, default=DEFAULT_KWH_PER_LITRE): vol.Coerce(
            float
        ),
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, data)

    try:
        # async_refresh() deliberately never raises: it records the failure on
        # the coordinator and returns. Catching around it therefore proves
        # nothing, and validation used to fall through and create an entry even
        # when the credentials were rejected. Inspect the outcome instead.
        await coordinator.async_refresh()

        if not coordinator.last_update_success:
            err = coordinator.last_exception
            if isinstance(err, BoilerJuiceAuthError):
                raise InvalidAuth from err
            if isinstance(err, BoilerJuiceConnectionError):
                raise CannotConnect from err
            if err is not None:
                raise err
            raise CannotConnect("BoilerJuice update failed for an unknown reason")

        # Get the model name if available, fallback to tank name, then default
        title = "BoilerJuice Tank"
        if coordinator.data:
            if coordinator.data.get("model"):
                title = coordinator.data["model"]
            elif coordinator.data.get("name"):
                title = coordinator.data["name"]

        return {"title": title}
    finally:
        # Each coordinator owns a private aiohttp session; always close the
        # throwaway one used for config-flow validation.
        await coordinator.async_close()


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BoilerJuice."""

    VERSION = 1

    async def async_step_import(
        self, import_config: dict[str, Any] | None
    ) -> FlowResult:
        """Import a config entry from configuration.yaml."""
        return await self.async_step_user(import_config)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                # A tank id is a numeric path segment. Anything else would
                # build a URL that resolves to a page we cannot parse.
                raw_tank_id = user_input.get(CONF_TANK_ID)
                if raw_tank_id:
                    tank_id = validate_tank_id(raw_tank_id)
                    if tank_id is None:
                        raise InvalidTankId
                    user_input[CONF_TANK_ID] = tank_id

                # Check if this email is already configured
                await self.async_set_unique_id(user_input[CONF_EMAIL])
                self._abort_if_unique_id_configured()

                info = await validate_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data=user_input)
            except InvalidTankId:
                errors[CONF_TANK_ID] = "invalid_tank_id"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except AbortFlow:
                # "already configured" and friends are flow control, not
                # errors. Swallowing them below turned a clean abort into
                # "Unexpected error" and re-showed the form.
                raise
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect to the service."""


class InvalidTankId(HomeAssistantError):
    """Error to indicate the configured tank id is not a BoilerJuice tank id."""
