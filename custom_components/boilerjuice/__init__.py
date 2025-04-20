"""The BoilerJuice integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceRegistry, async_get as async_get_device_registry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.typing import ConfigType

from .const import CONF_TANK_ID, DOMAIN
from .coordinator import BoilerJuiceDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_EMAIL): cv.string,
                vol.Optional(CONF_PASSWORD): cv.string,
                vol.Optional(CONF_TANK_ID): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

# Service schemas
SERVICE_RESET_CONSUMPTION = "reset_consumption"
SCHEMA_RESET_CONSUMPTION = vol.Schema({})

@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the BoilerJuice services."""
    if hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION):
        return

    async def async_handle_reset_consumption(call: ServiceCall) -> None:
        """Handle the service call to reset consumption."""
        for entry_id, coordinator in hass.data[DOMAIN].items():
            coordinator.reset_consumption()
            await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        async_handle_reset_consumption,
        schema=SCHEMA_RESET_CONSUMPTION
    )

@callback
def async_unload_services(hass: HomeAssistant) -> None:
    """Unload BoilerJuice services."""
    if not hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION):
        return

    hass.services.async_remove(DOMAIN, SERVICE_RESET_CONSUMPTION)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the BoilerJuice component."""
    hass.data.setdefault(DOMAIN, {})

    if DOMAIN in config:
        # If we have YAML config, create a config entry
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data=config[DOMAIN],
            )
        )

    async_setup_services(hass)
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BoilerJuice from a config entry."""
    try:
        coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)
        await coordinator.async_config_entry_first_refresh()

        if not coordinator.last_update_success:
            _LOGGER.error("Failed to setup BoilerJuice: %s", coordinator.last_exception)
            if isinstance(coordinator.last_exception, ConfigEntryAuthFailed):
                raise ConfigEntryAuthFailed
            raise ConfigEntryNotReady

        # Register device
        device_registry = async_get_device_registry(hass)
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, coordinator.data["id"])},
            name=coordinator.data.get("name", coordinator.data.get("model", "BoilerJuice Tank")),
            manufacturer=coordinator.data.get("manufacturer", "BoilerJuice"),
            model=coordinator.data.get("model"),
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.boilerjuice.com/uk",
        )

        hass.data[DOMAIN][entry.entry_id] = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

    except Exception as err:
        _LOGGER.exception("Error setting up BoilerJuice: %s", str(err))
        raise ConfigEntryNotReady from err

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            async_unload_services(hass)

    return unload_ok