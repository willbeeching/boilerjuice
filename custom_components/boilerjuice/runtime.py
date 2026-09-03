"""What one loaded BoilerJuice config entry carries at runtime."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .coordinator import BoilerJuiceDataUpdateCoordinator


@dataclass(slots=True)
class BoilerJuiceRuntimeData:
    """Everything a loaded entry needs while it is running."""

    coordinator: BoilerJuiceDataUpdateCoordinator


# Typed entry, so `entry.runtime_data.coordinator` is checkable rather than
# fished out of an untyped hass.data dictionary.
BoilerJuiceConfigEntry = ConfigEntry[BoilerJuiceRuntimeData]
