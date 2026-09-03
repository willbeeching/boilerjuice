"""The BoilerJuice integration."""

from __future__ import annotations

import logging
from typing import Any, Iterable

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import CONF_TANK_ID, DOMAIN
from .coordinator import BoilerJuiceDataUpdateCoordinator
from .helpers import normalise_email
from .runtime import BoilerJuiceConfigEntry, BoilerJuiceRuntimeData

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


def _loaded_coordinators(
    hass: HomeAssistant,
) -> dict[str, BoilerJuiceDataUpdateCoordinator]:
    """Return every loaded account's coordinator, keyed by config entry id."""
    return {
        entry.entry_id: entry.runtime_data.coordinator
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        if hasattr(entry, "runtime_data") and entry.runtime_data is not None
    }


def _tank_ids_for_devices(hass: HomeAssistant, device_ids: Iterable[str]) -> set[str]:
    """Return the BoilerJuice tank ids the given devices stand for."""
    registry = dr.async_get(hass)
    tank_ids: set[str] = set()
    for device_id in device_ids:
        device = registry.async_get(device_id)
        if device is None:
            continue
        tank_ids.update(
            identifier for domain, identifier in device.identifiers if domain == DOMAIN
        )
    return tank_ids


def _resolve_targets(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[BoilerJuiceDataUpdateCoordinator, str | None]]:
    """Return the (coordinator, tank id) pairs a service call operates on.

    Resolves every target selector Home Assistant supports (entity, device,
    area and label) plus the explicit ``entry_id``. A target that names
    nothing belonging to this integration raises rather than falling through
    to "every configured account" - these services rewrite stored consumption
    history, so an accidental fan-out is destructive and silent.

    A tank id of None means "every tank on that account", which is what an
    account-wide target such as a config entry id means.
    """
    coordinators = _loaded_coordinators(hass)
    if not coordinators:
        raise HomeAssistantError("No BoilerJuice accounts are currently loaded")

    targets = {key: _as_list(call.data.get(key)) for key in TARGET_KEYS}

    if not any(targets.values()):
        # No target at all. With a single account this is unambiguous; with
        # more than one, refuse rather than guess.
        if len(coordinators) == 1:
            return [(next(iter(coordinators.values())), None)]
        raise HomeAssistantError(
            "Several BoilerJuice accounts are configured, so this action needs "
            "a target (pick a BoilerJuice device, entity, area or label)"
        )

    entry_ids: set[str] = set()
    for entry_id in targets["entry_id"]:
        if entry_id not in coordinators:
            raise HomeAssistantError(
                f"No BoilerJuice integration loaded for entry_id {entry_id}"
            )
        entry_ids.add(entry_id)

    device_ids = set(targets["device_id"])
    device_ids |= _devices_for_entities(hass, targets["entity_id"])
    device_ids |= _devices_in_areas(hass, targets["area_id"])
    device_ids |= _devices_with_labels(hass, targets["label_id"])

    entry_ids |= _entry_ids_for_devices(hass, device_ids, coordinators)
    entry_ids |= _entry_ids_for_entities(hass, targets["entity_id"], coordinators)
    entry_ids |= _entry_ids_for_areas(hass, targets["area_id"], coordinators)
    entry_ids |= _entry_ids_for_labels(hass, targets["label_id"], coordinators)

    if not entry_ids:
        raise HomeAssistantError(
            "The target of this action does not include any BoilerJuice tank"
        )

    # A device or entity target names a specific tank; an entry id does not.
    named_tanks = _tank_ids_for_devices(hass, device_ids)

    resolved: list[tuple[BoilerJuiceDataUpdateCoordinator, str | None]] = []
    for entry_id in sorted(entry_ids):
        coordinator = coordinators[entry_id]
        tanks = [tank_id for tank_id in coordinator.tank_ids if tank_id in named_tanks]
        if tanks and entry_id not in targets["entry_id"]:
            resolved.extend((coordinator, tank_id) for tank_id in tanks)
        else:
            resolved.append((coordinator, None))
    return resolved


def _devices_for_entities(hass: HomeAssistant, entity_ids: Iterable[str]) -> set[str]:
    """Return the device ids behind the given entities."""
    registry = er.async_get(hass)
    devices: set[str] = set()
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        if entry is not None and entry.device_id:
            devices.add(entry.device_id)
    return devices


def _devices_in_areas(hass: HomeAssistant, area_ids: Iterable[str]) -> set[str]:
    """Return the device ids in the given areas."""
    registry = dr.async_get(hass)
    return {
        device.id
        for area_id in area_ids
        for device in dr.async_entries_for_area(registry, area_id)
    }


def _devices_with_labels(hass: HomeAssistant, label_ids: Iterable[str]) -> set[str]:
    """Return the device ids carrying the given labels."""
    registry = dr.async_get(hass)
    return {
        device.id
        for label_id in label_ids
        for device in dr.async_entries_for_label(registry, label_id)
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the BoilerJuice services."""
    if hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION):
        return

    async def async_handle_reset_consumption(call: ServiceCall) -> None:
        """Handle the service call to reset consumption."""
        for coordinator, tank_id in _resolve_targets(hass, call):
            await coordinator.async_reset_consumption(tank_id)
            await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        async_handle_reset_consumption,
        schema=RESET_CONSUMPTION_SCHEMA,
    )

    async def async_handle_set_consumption(call: ServiceCall) -> None:
        """Handle the service call to set consumption values."""
        for coordinator, tank_id in _resolve_targets(hass, call):
            if not coordinator.data:
                _LOGGER.warning(
                    "Skipping set_consumption: this BoilerJuice account has no "
                    "tank reading yet"
                )
                continue
            await coordinator.async_set_consumption(
                call.data["liters"], call.data.get("daily"), tank_id
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
    if DOMAIN in config:
        # YAML configuration is a migration path only: it starts an import
        # flow once and is then managed in the UI. It is deprecated and will
        # be removed; see the README.
        _LOGGER.warning(
            "Configuring BoilerJuice in configuration.yaml is deprecated. "
            "The settings have been imported; remove the boilerjuice block "
            "from configuration.yaml"
        )
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data=config[DOMAIN],
            )
        )

    async_setup_services(hass)
    return True


async def async_migrate_entry(
    hass: HomeAssistant, entry: BoilerJuiceConfigEntry
) -> bool:
    """Migrate an entry created by an older version.

    Entity and device identities are keyed by tank id, which has not changed,
    so nothing is renamed and no device is duplicated. Only the entry's own
    unique id moves, from the email as typed to a normalised form, so the
    same account cannot be added twice under different capitalisation.
    """
    if entry.version > 2:
        return False

    if entry.version == 1:
        email = entry.data.get(CONF_EMAIL)
        hass.config_entries.async_update_entry(
            entry,
            unique_id=normalise_email(email) if email else entry.unique_id,
            version=2,
        )
        _LOGGER.debug("Migrated the BoilerJuice config entry to version 2")

    return True


async def async_setup_entry(hass: HomeAssistant, entry: BoilerJuiceConfigEntry) -> bool:
    """Set up BoilerJuice from a config entry."""
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    # Raises ConfigEntryNotReady, or ConfigEntryAuthFailed (which starts a
    # reauth flow), by itself.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = BoilerJuiceRuntimeData(coordinator=coordinator)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    async_setup_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_reload_entry(
    hass: HomeAssistant, entry: BoilerJuiceConfigEntry
) -> None:
    """Reload after the user changes the credentials or the options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: BoilerJuiceConfigEntry
) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None:
        await coordinator.coordinator.async_close()

    if not hass.config_entries.async_loaded_entries(DOMAIN):
        async_unload_services(hass)
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: BoilerJuiceConfigEntry
) -> None:
    """Delete this account's stored consumption history when it is removed."""
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)
    await coordinator.async_remove_storage()
