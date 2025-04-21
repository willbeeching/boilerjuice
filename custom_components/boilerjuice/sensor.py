"""Sensor platform for BoilerJuice."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfVolume,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.device_registry import DeviceEntryType
from typing import Any

from .const import (
    DOMAIN,
    SENSOR_VOLUME,
    SENSOR_CAPACITY,
    SENSOR_HEIGHT,
    UNIT_PERCENTAGE,
    UNIT_LITRES,
    UNIT_CM,
    ATTR_TANK_NAME,
    ATTR_TANK_SHAPE,
    ATTR_OIL_TYPE,
    ATTR_TANK_MODEL,
    ATTR_TANK_ID,
    DEFAULT_KWH_PER_LITRE,
)
from .coordinator import BoilerJuiceDataUpdateCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BoilerJuice sensors from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Add the consumption reset service
    async def handle_reset_consumption(call: ServiceCall) -> None:
        """Handle the service call to reset consumption."""
        coordinator.reset_consumption()
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        "reset_consumption",
        handle_reset_consumption,
    )

    async_add_entities(
        [
            BoilerJuiceTotalOilLevelSensor(coordinator, entry.entry_id),
            BoilerJuiceUsableOilLevelSensor(coordinator, entry.entry_id),
            BoilerJuiceTankVolumeSensor(coordinator, entry.entry_id),
            BoilerJuiceUsableVolumeSensor(coordinator, entry.entry_id),
            BoilerJuiceTankCapacitySensor(coordinator, entry.entry_id),
            BoilerJuiceDailyConsumptionSensor(coordinator, entry.entry_id),
            BoilerJuiceTotalConsumptionSensor(coordinator, entry.entry_id),
            BoilerJuiceTotalConsumptionKwhSensor(coordinator, entry.entry_id),
            BoilerJuiceTankHeightSensor(coordinator, entry.entry_id),
            BoilerJuiceDaysUntilEmptySensor(coordinator, entry.entry_id),
            BoilerJuiceKwhPerLitreSensor(coordinator, entry.entry_id),
            BoilerJuiceCostPerKwhSensor(coordinator, entry.entry_id),
            BoilerJuiceOilPriceSensor(coordinator, entry.entry_id),
        ]
    )

class BoilerJuiceSensor(SensorEntity):
    """Base class for BoilerJuice sensors."""

    def __init__(
        self,
        coordinator: BoilerJuiceDataUpdateCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the sensor."""
        self._coordinator = coordinator
        self._attr_has_entity_name = True
        self._attr_should_poll = False
        self._attr_unique_id = f"{coordinator.data['id']}_{self.__class__.__name__}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.data["id"])},
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._coordinator.last_update_success

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )

    async def async_update(self) -> None:
        """Update the entity."""
        await self._coordinator.async_request_refresh()

class BoilerJuiceTankVolumeSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice tank volume sensor."""

    _attr_name = SENSOR_VOLUME
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_device_class = SensorDeviceClass.VOLUME
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        return self._coordinator.data.get("current_volume_litres")

class BoilerJuiceUsableVolumeSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice usable oil volume sensor."""

    _attr_name = "Usable Oil Volume"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_device_class = SensorDeviceClass.VOLUME
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        return self._coordinator.data.get("usable_volume_litres")

class BoilerJuiceTankCapacitySensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice tank capacity sensor."""

    _attr_name = SENSOR_CAPACITY
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_device_class = SensorDeviceClass.VOLUME
    _attr_state_class = None

    @property
    def native_value(self) -> int | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        return self._coordinator.data.get("capacity_litres")

class BoilerJuiceTankHeightSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice tank height sensor."""

    _attr_name = SENSOR_HEIGHT
    _attr_native_unit_of_measurement = "cm"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        return self._coordinator.data.get("height_cm")

class BoilerJuiceDailyConsumptionSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice daily consumption sensor."""

    _attr_name = "Daily Oil Consumption"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_device_class = SensorDeviceClass.VOLUME
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        value = self._coordinator.data.get("daily_consumption_usable_liters")
        return round(value, 1) if value is not None else None

class BoilerJuiceTotalConsumptionSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice total consumption sensor."""

    _attr_name = "Total Oil Consumption"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_device_class = SensorDeviceClass.VOLUME
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        value = self._coordinator.data.get("total_consumption_usable_liters")
        return round(value, 1) if value is not None else None

class BoilerJuiceTotalConsumptionKwhSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice total consumption in kWh sensor."""

    _attr_name = "Total Oil Consumption (kWh)"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        value = self._coordinator.data.get("total_consumption_usable_kwh")
        return round(value, 1) if value is not None else None

class BoilerJuiceDaysUntilEmptySensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice days until empty sensor."""

    _attr_name = "Days Until Empty"
    _attr_native_unit_of_measurement = "days"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None

        usable_volume = self._coordinator.data.get("usable_volume_litres")
        if usable_volume is None:
            return None

        # If we have actual consumption data, use it
        daily_consumption = self._coordinator.data.get("daily_consumption_usable_liters")
        if daily_consumption and daily_consumption > 0:
            return round(usable_volume / daily_consumption, 1)

        # Otherwise, estimate based on usable capacity
        usable_capacity = self._coordinator.data.get("usable_capacity_litres", 510)  # Default to 510L if not specified
        if usable_capacity:
            # Assume average daily consumption of 2% of usable capacity
            estimated_daily_consumption = usable_capacity * 0.02
            return round(usable_volume / estimated_daily_consumption, 1)

        return None

class BoilerJuiceUsableOilLevelSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice usable oil level sensor."""

    _attr_name = "Usable Oil Level"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        return self._coordinator.data.get("usable_level_percentage")

class BoilerJuiceTotalOilLevelSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice total oil level sensor."""

    _attr_name = "Total Oil Level"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        return self._coordinator.data.get("total_level_percentage")

class BoilerJuiceOilPriceSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice oil price sensor."""

    _attr_name = "BoilerJuice Oil Price"
    _attr_native_unit_of_measurement = "GBP/litre"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:currency-gbp"

    @property
    def native_value(self) -> float | None:
        """Return the current oil price in GBP per litre."""
        if not self._coordinator.data:
            return None
        price_pence = self._coordinator.data.get("current_price_pence")
        if price_pence is None:
            return None
        return round(price_pence / 100, 2)  # Convert pence to pounds

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        if not self._coordinator.data:
            return {}
        return {
            "price_pence_per_litre": self._coordinator.data.get("current_price_pence"),
            "last_updated": self._coordinator.data.get("last_updated")
        }

class BoilerJuiceKwhPerLitreSensor(BoilerJuiceSensor):
    """Representation of the kWh per litre conversion factor."""

    _attr_name = "Oil Energy Content"
    _attr_native_unit_of_measurement = "kWh/L"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash"

    @property
    def native_value(self) -> float:
        """Return the kWh per litre conversion factor."""
        return self._coordinator.data.get("kwh_per_litre", DEFAULT_KWH_PER_LITRE)

class BoilerJuiceCostPerKwhSensor(BoilerJuiceSensor):
    """Representation of the cost per kWh based on current oil price."""

    _attr_name = "Oil Cost per kWh"
    _attr_native_unit_of_measurement = "GBP/kWh"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:currency-gbp"

    @property
    def native_value(self) -> float | None:
        """Return the cost per kWh."""
        if not self._coordinator.data:
            return None

        price_pence = self._coordinator.data.get("current_price_pence")
        kwh_per_litre = self._coordinator.data.get("kwh_per_litre", DEFAULT_KWH_PER_LITRE)

        if price_pence is None or kwh_per_litre is None:
            return None

        # Convert pence/litre to pounds/kWh
        # First convert pence to pounds (divide by 100)
        # Then divide by kWh per litre to get pounds per kWh
        return round((price_pence / 100) / kwh_per_litre, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        if not self._coordinator.data:
            return {}
        return {
            "price_pence_per_litre": self._coordinator.data.get("current_price_pence"),
            "kwh_per_litre": self._coordinator.data.get("kwh_per_litre", DEFAULT_KWH_PER_LITRE),
            "last_updated": self._coordinator.data.get("last_updated")
        }