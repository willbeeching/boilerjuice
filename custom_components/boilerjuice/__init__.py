"""The BoilerJuice integration."""

from __future__ import annotations

import logging
from typing import Any, Iterable

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
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
SERVICE_SET_CONSUMPTION = "set_consumption"

# Target selectors Home Assistant injects when the user picks a target in the
# UI, plus the explicit entry_id escape hatch. Every one of these is resolved
# to a config entry below; anything left unresolved is an error rather than a
# silent "apply to everything".
TARGET_KEYS = ("entry_id", "device_id", "entity_id", "area_id", "label_id")

RESET_CONSUMPTION_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): vol.Any(cv.string, [cv.string]),
    },
    extra=vol.ALLOW_EXTRA,
)

SET_CONSUMPTION_SCHEMA = vol.Schema(
    {
        vol.Required("liters"): cv.positive_float,
        vol.Optional("daily"): cv.positive_float,
        vol.Optional("entry_id"): vol.Any(cv.string, [cv.string]),
    },
    extra=vol.ALLOW_EXTRA,
)


def _as_list(value: Any) -> list[str]:
    """Normalise a target field that may be a string or a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _entry_ids_for_devices(
    hass: HomeAssistant, device_ids: Iterable[str], known: dict
) -> set[str]:
    """Return the BoilerJuice config entries owning `device_ids`."""
    device_registry = dr.async_get(hass)
    entry_ids: set[str] = set()
    for device_id in device_ids:
        device = device_registry.async_get(device_id)
        if device is None:
            raise HomeAssistantError(f"Unknown device_id {device_id}")
        entry_ids.update(
            entry_id for entry_id in device.config_entries if entry_id in known
        )
    return entry_ids


def _entry_ids_for_entities(
    hass: HomeAssistant, entity_ids: Iterable[str], known: dict
) -> set[str]:
    """Return the BoilerJuice config entries owning `entity_ids`."""
    entity_registry = er.async_get(hass)
    entry_ids: set[str] = set()
    for entity_id in entity_ids:
        entry = entity_registry.async_get(entity_id)
        if entry is None:
            raise HomeAssistantError(f"Unknown entity_id {entity_id}")
        if entry.config_entry_id in known:
            entry_ids.add(entry.config_entry_id)
    return entry_ids


def _entry_ids_for_areas(
    hass: HomeAssistant, area_ids: Iterable[str], known: dict
) -> set[str]:
    """Return the BoilerJuice config entries with devices or entities in `area_ids`."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    entry_ids: set[str] = set()
    for area_id in area_ids:
        for device in dr.async_entries_for_area(device_registry, area_id):
            entry_ids.update(
                entry_id for entry_id in device.config_entries if entry_id in known
            )
        for entity in er.async_entries_for_area(entity_registry, area_id):
            if entity.config_entry_id in known:
                entry_ids.add(entity.config_entry_id)
    return entry_ids


def _entry_ids_for_labels(
    hass: HomeAssistant, label_ids: Iterable[str], known: dict
) -> set[str]:
    """Return the BoilerJuice config entries carrying `label_ids`."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    entry_ids: set[str] = set()
    for label_id in label_ids:
        for device in dr.async_entries_for_label(device_registry, label_id):
            entry_ids.update(
                entry_id for entry_id in device.config_entries if entry_id in known
            )
        for entity in er.async_entries_for_label(entity_registry, label_id):
            if entity.config_entry_id in known:
                entry_ids.add(entity.config_entry_id)
    return entry_ids


def _resolve_target_coordinators(
    hass: HomeAssistant, call: ServiceCall
) -> list[BoilerJuiceDataUpdateCoordinator]:
    """Return the coordinators a service call should operate on.

    Resolves every target selector Home Assistant supports (entity, device,
    area and label) plus the explicit ``entry_id``. A target that names
    nothing belonging to this integration raises rather than falling through
    to "every configured account" - these services rewrite stored consumption
    history, so an accidental fan-out is destructive and silent.
    """
    coordinators_by_entry: dict[str, BoilerJuiceDataUpdateCoordinator] = hass.data.get(
        DOMAIN, {}
    )
    if not coordinators_by_entry:
        raise HomeAssistantError("No BoilerJuice accounts are currently loaded")

    targets = {key: _as_list(call.data.get(key)) for key in TARGET_KEYS}

    if not any(targets.values()):
        # No target at all. With a single account this is unambiguous; with
        # more than one, refuse rather than guess.
        if len(coordinators_by_entry) == 1:
            return list(coordinators_by_entry.values())
        raise HomeAssistantError(
            "Several BoilerJuice accounts are configured, so this action needs "
            "a target (pick a BoilerJuice device, entity, area or label)"
        )

    entry_ids: set[str] = set()
    for entry_id in targets["entry_id"]:
        if entry_id not in coordinators_by_entry:
            raise HomeAssistantError(
                f"No BoilerJuice integration loaded for entry_id {entry_id}"
            )
        entry_ids.add(entry_id)

    entry_ids |= _entry_ids_for_devices(
        hass, targets["device_id"], coordinators_by_entry
    )
    entry_ids |= _entry_ids_for_entities(
        hass, targets["entity_id"], coordinators_by_entry
    )
    entry_ids |= _entry_ids_for_areas(hass, targets["area_id"], coordinators_by_entry)
    entry_ids |= _entry_ids_for_labels(hass, targets["label_id"], coordinators_by_entry)

    if not entry_ids:
        raise HomeAssistantError(
            "The target of this action does not include any BoilerJuice tank"
        )

    return [coordinators_by_entry[entry_id] for entry_id in sorted(entry_ids)]


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the BoilerJuice services."""
    if hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION):
        return

    async def async_handle_reset_consumption(call: ServiceCall) -> None:
        """Handle the service call to reset consumption."""
        for coordinator in _resolve_target_coordinators(hass, call):
            await coordinator.async_reset_consumption()
            await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        async_handle_reset_consumption,
        schema=RESET_CONSUMPTION_SCHEMA,
    )

    async def async_handle_set_consumption(call: ServiceCall) -> None:
        """Handle the service call to set consumption values."""
        for coordinator in _resolve_target_coordinators(hass, call):
            if not coordinator.data:
                _LOGGER.warning(
                    "Skipping set_consumption: this BoilerJuice account has no "
                    "tank reading yet"
                )
                continue
            await coordinator.async_set_consumption(
                call.data["liters"], call.data.get("daily")
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
    hass.services.async_remove(DOMAIN, SERVICE_SET_CONSUMPTION)


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

    # Raises ConfigEntryNotReady (or ConfigEntryAuthFailed) by itself, so
    # there is nothing to re-check afterwards.
    await coordinator.async_config_entry_first_refresh()

    # Register device
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, coordinator.data["id"])},
        name=coordinator.data.get(
            "name", coordinator.data.get("model", "BoilerJuice Tank")
        ),
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
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_close()
        if not hass.data[DOMAIN]:
            async_unload_services(hass)
            hass.data.pop(DOMAIN)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete this account's stored consumption history when it is removed."""
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)
    await coordinator.async_remove_storage()
