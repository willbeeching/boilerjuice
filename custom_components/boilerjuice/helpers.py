"""Small shared helpers."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN


def normalise_email(email: str) -> str:
    """Return an email in the form used as a config entry's unique id.

    BoilerJuice treats sign-in addresses case-insensitively, so "Me@Example.com"
    and "me@example.com" are one account and must not be addable twice.
    """
    return email.strip().lower()


def async_tank_device(
    hass: HomeAssistant, tank_id: str, entry_id: str
) -> dr.DeviceEntry | None:
    """Return the device for one tank, across supported Home Assistant versions.

    Home Assistant 2026.9 made device identifiers unique per config entry
    rather than globally, deprecating `async_get_device(identifiers=...)` in
    favour of `async_get_device_by_identifier(identifier, config_entry_id)`.
    The old call raises in tests on current Home Assistant and is scheduled
    for removal, while the new one does not exist on the supported floor, so
    both are needed until the floor moves past 2026.9.
    """
    registry = dr.async_get(hass)
    lookup = getattr(registry, "async_get_device_by_identifier", None)
    if lookup is not None:
        return lookup((DOMAIN, tank_id), entry_id)  # type: ignore[no-any-return]
    return registry.async_get_device(identifiers={(DOMAIN, tank_id)})


def device_config_entry_ids(device: Any) -> set[str]:
    """Return the config entries a device belongs to.

    Since 2026.9 a device belongs to exactly one config entry, exposed as
    `config_entry_id`; `config_entries` survives as a deprecated compatibility
    property. Prefer the singular form where it exists.
    """
    entry_id = getattr(device, "config_entry_id", None)
    if entry_id is not None:
        return {entry_id}
    return set(device.config_entries)


def device_tank_ids(device: Any) -> set[str]:
    """Return the BoilerJuice tank ids a device stands for."""
    return {identifier for domain, identifier in device.identifiers if domain == DOMAIN}
