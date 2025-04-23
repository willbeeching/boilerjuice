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
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType
from typing import Any, Dict
from datetime import datetime
import datetime as dt
from zoneinfo import ZoneInfo
import logging

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

_LOGGER = logging.getLogger(__name__)

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
            BoilerJuiceIncrementalConsumptionKwhSensor(coordinator, entry.entry_id),
            BoilerJuiceTankHeightSensor(coordinator, entry.entry_id),
            BoilerJuiceDaysUntilEmptySensor(coordinator, entry.entry_id),
            BoilerJuiceKwhPerLitreSensor(coordinator, entry.entry_id),
            BoilerJuiceCostPerKwhSensor(coordinator, entry.entry_id),
            BoilerJuiceOilPriceSensor(coordinator, entry.entry_id),
            BoilerJuiceLastUpdateSensor(coordinator, entry.entry_id),
            BoilerJuiceSeasonalConsumptionSensor(coordinator, entry.entry_id),
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

class BoilerJuiceIncrementalConsumptionKwhSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice incremental consumption in kWh sensor."""

    _attr_name = "Oil Consumption (kWh)"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> float | None:
        """Return the incremental energy consumption in kWh."""
        if not self._coordinator.data:
            return None

        # Get the daily consumption in liters
        daily_consumption_liters = self._coordinator.data.get("daily_consumption_usable_liters")
        if daily_consumption_liters is None:
            return None

        # Get the last update time
        last_update = self._coordinator._last_update
        if last_update is None:
            return None

        # Get Tado heating state
        try:
            tado_state = self.hass.states.get("climate.heating")  # Adjust this entity ID to match your Tado zone
            if tado_state and tado_state.state == "heat":
                # If heating is on, report the full daily consumption
                daily_consumption_kwh = daily_consumption_liters * 10.35
                return round(daily_consumption_kwh, 1)
            else:
                # If heating is off, report no consumption
                return 0.0
        except Exception as e:
            _LOGGER.warning("Failed to get Tado state, falling back to time-based distribution: %s", e)

            # Fallback to time-based distribution if Tado data is unavailable
            now = datetime.now(last_update.tzinfo)
            seconds_since_update = (now - last_update).total_seconds()
            day_progress = min(1.0, max(0.0, seconds_since_update / (24 * 3600)))

            # Convert liters to kWh using the standard conversion factor
            daily_consumption_kwh = daily_consumption_liters * 10.35
            current_consumption = daily_consumption_kwh * day_progress

            return round(current_consumption, 1)

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
    """Representation of a BoilerJuice cost per kWh sensor."""

    _attr_name = "Oil Cost Per kWh"
    _attr_native_unit_of_measurement = "p/kWh"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:currency-gbp"

    @property
    def native_value(self) -> float | None:
        """Return the cost per kWh."""
        if not self._coordinator.data:
            return None

        oil_price = self._coordinator.data.get("current_price_pence")
        kwh_per_litre = self._coordinator.data.get("kwh_per_litre")

        if oil_price is None or kwh_per_litre is None or kwh_per_litre == 0:
            return None

        cost_per_kwh = oil_price / kwh_per_litre
        return round(cost_per_kwh, 1)

class BoilerJuiceLastUpdateSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice last update time sensor."""

    _attr_name = "Last Updated"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> datetime | None:
        """Return the last update timestamp."""
        if not self._coordinator._last_update:
            return None

        # Make sure timestamp has timezone info
        last_update = self._coordinator._last_update
        if last_update.tzinfo is None:
            # Use the Home Assistant timezone
            return last_update.replace(tzinfo=ZoneInfo(self.hass.config.time_zone))
        return last_update

class BoilerJuiceSeasonalConsumptionSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice seasonal consumption sensor."""

    _attr_name = "Seasonal Oil Consumption"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_device_class = SensorDeviceClass.VOLUME
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        """Return the current season's average daily consumption."""
        if not self._coordinator.data:
            return None
        seasonal_stats = self._coordinator.data.get("seasonal_stats", {})
        current_season = seasonal_stats.get("current_season", {})
        return current_season.get("avg")

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return seasonal consumption statistics."""
        if not self._coordinator.data:
            return {}

        seasonal_stats = self._coordinator.data.get("seasonal_stats", {})
        if not seasonal_stats:
            return {}

        attributes = {
            "current_season": seasonal_stats.get("current_season", {}).get("name", "unknown"),
            "current_season_min": seasonal_stats.get("current_season", {}).get("min", 0),
            "current_season_max": seasonal_stats.get("current_season", {}).get("max", 0),
            "winter_average": seasonal_stats.get("winter_avg", 0),
            "spring_average": seasonal_stats.get("spring_avg", 0),
            "summer_average": seasonal_stats.get("summer_avg", 0),
            "autumn_average": seasonal_stats.get("autumn_avg", 0),
        }

        # Add monthly averages if available
        monthly = seasonal_stats.get("monthly", {})
        if monthly:
            attributes["monthly_averages"] = monthly

        return attributes