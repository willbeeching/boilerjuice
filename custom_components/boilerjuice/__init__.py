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

from .const import CONF_TANK_ID, DOMAIN, CONF_KWH_PER_LITRE, DEFAULT_KWH_PER_LITRE
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
SERVICE_SET_CONSUMPTION = "set_consumption"
RESET_CONSUMPTION_SCHEMA = vol.Schema({})

SET_CONSUMPTION_SCHEMA = vol.Schema({
    vol.Required("liters"): cv.positive_float,
    vol.Optional("daily"): cv.positive_float,
})

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
        schema=RESET_CONSUMPTION_SCHEMA
    )

    async def async_handle_set_consumption(call: ServiceCall) -> None:
        """Handle the service call to set consumption values."""
        data = dict(call.data)
        total_consumption = data["liters"]
        daily_consumption = data.get("daily")

        for entry_id, coordinator in hass.data[DOMAIN].items():
            if coordinator.data:
                # Set the consumption values
                coordinator._total_consumption_usable_liters = total_consumption
                coordinator._total_consumption_usable_kwh = total_consumption * 10.35  # Use standard conversion

                if daily_consumption:
                    coordinator._daily_consumption_usable_liters = daily_consumption

                # Force using current values as reference
                coordinator.force_consumption_reference(coordinator.data)

                # Update the consumption data in the current data
                coordinator.data["total_consumption_usable_liters"] = total_consumption
                coordinator.data["total_consumption_usable_kwh"] = total_consumption * 10.35

                if daily_consumption:
                    coordinator.data["daily_consumption_usable_liters"] = daily_consumption

                # Force a refresh to update the UI
                coordinator.async_set_updated_data(coordinator.data)

                _LOGGER.info(
                    "Manually set consumption values: total=%s L (%s kWh), daily=%s L/day",
                    total_consumption,
                    round(total_consumption * 10.35, 1),
                    daily_consumption or "unchanged"
                )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CONSUMPTION,
        async_handle_set_consumption,
        schema=SET_CONSUMPTION_SCHEMA,
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
    # Initialize the domain data if it doesn't exist
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    # Fetch initial data
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

    # Ensure services are set up
    async_setup_services(hass)

    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            async_unload_services(hass)
            hass.data.pop(DOMAIN)
    return unload_ok