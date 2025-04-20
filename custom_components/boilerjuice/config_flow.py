"""Config flow for BoilerJuice."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_TANK_ID, DOMAIN
from .coordinator import BoilerJuiceDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_TANK_ID): str,
    }
)

async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, data)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        if "Invalid credentials" in str(err):
            raise InvalidAuth from err
        if "Failed to login" in str(err):
            raise CannotConnect from err
        raise err

    # Get the tank name if available
    title = "BoilerJuice Tank"
    if coordinator.data and coordinator.data.get("name"):
        title = coordinator.data["name"]

    return {"title": title}

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BoilerJuice."""

    VERSION = 1

    async def async_step_import(self, import_config: dict[str, Any] | None) -> FlowResult:
        """Import a config entry from configuration.yaml."""
        return await self.async_step_user(import_config)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                # Check if this email is already configured
                await self.async_set_unique_id(user_input[CONF_EMAIL])
                self._abort_if_unique_id_configured()

                info = await validate_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data=user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
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