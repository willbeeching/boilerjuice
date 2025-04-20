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

from .const import (
    DOMAIN,
    SENSOR_LEVEL,
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
            BoilerJuiceTankLevelSensor(coordinator, entry.entry_id),
            BoilerJuiceTankVolumeSensor(coordinator, entry.entry_id),
            BoilerJuiceTankCapacitySensor(coordinator, entry.entry_id),
            BoilerJuiceTankHeightSensor(coordinator, entry.entry_id),
            BoilerJuiceDailyConsumptionSensor(coordinator, entry.entry_id),
            BoilerJuiceTotalConsumptionSensor(coordinator, entry.entry_id),
            BoilerJuiceTotalConsumptionKwhSensor(coordinator, entry.entry_id),
            BoilerJuiceDaysUntilEmptySensor(coordinator, entry.entry_id),
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
        self._attr_unique_id = f"{entry_id}_{self.__class__.__name__}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
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

class BoilerJuiceTankLevelSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice tank level sensor."""

    _attr_name = SENSOR_LEVEL
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        return self._coordinator.data.get("level_percentage")

    @property
    def extra_state_attributes(self) -> dict:
        """Return the state attributes."""
        if not self._coordinator.data:
            return {}
        return {
            ATTR_TANK_NAME: self._coordinator.data.get("name"),
            ATTR_TANK_SHAPE: self._coordinator.data.get("shape"),
            ATTR_OIL_TYPE: self._coordinator.data.get("oil_type"),
            ATTR_TANK_MODEL: self._coordinator.data.get("model"),
            ATTR_TANK_ID: self._coordinator.data.get("id"),
        }

class BoilerJuiceTankVolumeSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice tank volume sensor."""

    _attr_name = SENSOR_VOLUME
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_device_class = SensorDeviceClass.VOLUME
    _attr_state_class = None

    @property
    def native_value(self) -> int | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        return self._coordinator.data.get("current_volume_litres")

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
    _attr_state_class = None

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        value = self._coordinator.data.get("daily_consumption_liters")
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
        value = self._coordinator.data.get("total_consumption_liters")
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
        value = self._coordinator.data.get("total_consumption_kwh")
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
        value = self._coordinator.data.get("days_until_empty")
        return round(value, 1) if value is not None else None