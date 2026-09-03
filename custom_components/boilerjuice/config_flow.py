"""Config, reauth and reconfigure flows for BoilerJuice."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .client import BoilerJuiceClient
from .const import (
    CONF_KWH_PER_LITRE,
    CONF_TANK_ID,
    CONF_TANKS,
    DEFAULT_KWH_PER_LITRE,
    DOMAIN,
)
from .errors import BoilerJuiceAuthError, BoilerJuiceConnectionError, BoilerJuiceError
from .helpers import normalise_email
from .parser import validate_tank_id

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_KWH_PER_LITRE, default=DEFAULT_KWH_PER_LITRE): vol.Coerce(
            float
        ),
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


async def async_validate_account(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[str, list[str]]:
    """Sign in and list the account's tanks.

    Returns the entry title and the tank ids found. Raises InvalidAuth or
    CannotConnect so the caller can put the right message on the form.
    """
    import aiohttp
    from homeassistant.helpers.aiohttp_client import async_create_clientsession

    client = BoilerJuiceClient(
        lambda timeout: async_create_clientsession(
            hass, cookie_jar=aiohttp.CookieJar(), timeout=timeout
        ),
        data[CONF_EMAIL],
        data[CONF_PASSWORD],
    )

    try:
        tank_ids = await client.async_list_tank_ids()
    except BoilerJuiceAuthError as err:
        raise InvalidAuth from err
    except (BoilerJuiceConnectionError, BoilerJuiceError) as err:
        raise CannotConnect from err
    finally:
        await client.async_close()

    if not tank_ids:
        raise NoTanks

    return normalise_email(data[CONF_EMAIL]), tank_ids


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BoilerJuice."""

    VERSION = 2

    def __init__(self) -> None:
        """Start with no gathered input."""
        self._input: dict[str, Any] = {}
        self._tank_ids: list[str] = []

    # ------------------------------------------------------------------
    # Adding an account
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self.async_set_unique_id(normalise_email(user_input[CONF_EMAIL]))
                self._abort_if_unique_id_configured()

                title, tank_ids = await async_validate_account(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except NoTanks:
                errors["base"] = "no_tanks"
            except AbortFlow:
                # "already configured" and friends are flow control, not
                # errors. Swallowing them below turned a clean abort into
                # "Unexpected error" and re-showed the form.
                raise
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                self._input = dict(user_input)
                self._tank_ids = tank_ids
                return self.async_create_entry(title=title, data=self._input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_import(
        self, import_config: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Import an entry from configuration.yaml.

        YAML is a migration path only. A tank id from the old configuration
        is carried across so an existing single-tank install keeps tracking
        exactly the tank it was.
        """
        config = dict(import_config or {})
        tank_id = validate_tank_id(config.pop(CONF_TANK_ID, None))
        result = await self.async_step_user(config)
        if tank_id and result["type"] == "create_entry":
            result["data"] = {**result["data"], CONF_TANK_ID: tank_id}
        return result

    # ------------------------------------------------------------------
    # Fixing credentials
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after BoilerJuice rejected the session."""
        self._input = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the current password and confirm it works."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            candidate = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await async_validate_account(self.hass, candidate)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except NoTanks:
                errors["base"] = "no_tanks"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data=candidate)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"email": entry.data.get(CONF_EMAIL, "")},
        )

    # ------------------------------------------------------------------
    # Changing settings
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the credentials, the tracked tanks or the energy content."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            candidate = {
                CONF_EMAIL: entry.data[CONF_EMAIL],
                CONF_PASSWORD: user_input.get(CONF_PASSWORD)
                or entry.data[CONF_PASSWORD],
            }
            try:
                _, tank_ids = await async_validate_account(self.hass, candidate)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except NoTanks:
                errors["base"] = "no_tanks"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                chosen = [
                    tank_id
                    for tank_id in user_input.get(CONF_TANKS, [])
                    if tank_id in tank_ids
                ]
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, **candidate},
                    options={
                        **entry.options,
                        CONF_KWH_PER_LITRE: user_input[CONF_KWH_PER_LITRE],
                        CONF_TANKS: chosen,
                    },
                )

        try:
            _, tank_ids = await async_validate_account(self.hass, dict(entry.data))
        except (InvalidAuth, CannotConnect, NoTanks):
            # The account is unreachable right now; offer the tanks we know.
            tank_ids = list(entry.options.get(CONF_TANKS) or [])

        current = entry.options.get(CONF_TANKS) or tank_ids

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PASSWORD): str,
                    vol.Required(
                        CONF_KWH_PER_LITRE,
                        default=entry.options.get(
                            CONF_KWH_PER_LITRE,
                            entry.data.get(CONF_KWH_PER_LITRE, DEFAULT_KWH_PER_LITRE),
                        ),
                    ): vol.Coerce(float),
                    vol.Optional(CONF_TANKS, default=current): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=tank_id, label=f"Tank {tank_id}")
                                for tank_id in tank_ids
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            errors=errors,
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect to the service."""


class NoTanks(HomeAssistantError):
    """Error to indicate the account has no tanks to track."""
