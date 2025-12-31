"""Sensor platform for BoilerJuice."""

from __future__ import annotations

import datetime as dt
import logging
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import (
    ATTR_OIL_TYPE,
    ATTR_TANK_ID,
    ATTR_TANK_MODEL,
    ATTR_TANK_NAME,
    ATTR_TANK_SHAPE,
    DEFAULT_KWH_PER_LITRE,
    DOMAIN,
    SENSOR_CAPACITY,
    SENSOR_HEIGHT,
    SENSOR_VOLUME,
    UNIT_CM,
    UNIT_LITRES,
    UNIT_PERCENTAGE,
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
            # Simplified sensors - BoilerJuice now only provides one oil level (not separate total/usable)
            BoilerJuiceOilLevelSensor(coordinator, entry.entry_id),
            BoilerJuiceTankVolumeSensor(coordinator, entry.entry_id),
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
    """Representation of a BoilerJuice tank volume sensor.

    Note: BoilerJuice simplified their interface and now only provides one volume
    (previously they had separate "current" and "usable" volumes).
    """

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
    _attr_native_unit_of_measurement = "L/day"
    _attr_icon = "mdi:gauge"

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

    def __init__(
        self,
        coordinator: BoilerJuiceDataUpdateCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry_id)
        self._last_consumption = 0.0
        self._last_check_time = None
        self._daily_consumption = 0.0
        self._last_reset = None

    @property
    def native_value(self) -> float | None:
        """Return the incremental energy consumption in kWh."""
        if not self._coordinator.data:
            return None

        now = datetime.now(ZoneInfo(self.hass.config.time_zone))

        # Reset at midnight
        if self._last_reset is None or now.date() != self._last_reset.date():
            self._daily_consumption = 0.0
            self._last_reset = now
            self._last_check_time = now
            return self._daily_consumption

        # Get the daily consumption in liters
        daily_consumption_liters = self._coordinator.data.get(
            "daily_consumption_usable_liters"
        )
        if daily_consumption_liters is None:
            return self._daily_consumption

        if self._last_check_time:
            # Calculate consumption since last check based on time
            time_diff = (now - self._last_check_time).total_seconds() / (
                24 * 3600
            )  # Fraction of day
            kwh_per_litre = self._coordinator.data.get("kwh_per_litre", 10.35)
            incremental_consumption = (
                daily_consumption_liters * kwh_per_litre
            ) * time_diff
            self._daily_consumption += incremental_consumption

        self._last_check_time = now
        return round(self._daily_consumption, 1)


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
        daily_consumption = self._coordinator.data.get(
            "daily_consumption_usable_liters"
        )
        if daily_consumption and daily_consumption > 0:
            return round(usable_volume / daily_consumption, 1)

        # Otherwise, estimate based on usable capacity
        usable_capacity = self._coordinator.data.get(
            "usable_capacity_litres", 510
        )  # Default to 510L if not specified
        if usable_capacity:
            # Assume average daily consumption of 2% of usable capacity
            estimated_daily_consumption = usable_capacity * 0.02
            return round(usable_volume / estimated_daily_consumption, 1)

        return None


class BoilerJuiceOilLevelSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice oil level sensor.

    Note: BoilerJuice simplified their interface and now only provides one oil level
    (previously they had separate "total" and "usable" levels).
    They now call it "total oil remaining".
    """

    _attr_name = "Oil Level"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self._coordinator.data:
            return None
        # BoilerJuice now provides one level called "total oil remaining"
        return self._coordinator.data.get("total_level_percentage")


class BoilerJuiceOilPriceSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice oil price sensor."""

    _attr_name = "BoilerJuice Oil Price"
    _attr_native_unit_of_measurement = "GBP/litre"
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
            "last_updated": self._coordinator.data.get("last_updated"),
        }


class BoilerJuiceKwhPerLitreSensor(BoilerJuiceSensor):
    """Representation of the kWh per litre conversion factor."""

    _attr_name = "Oil Energy Content"
    _attr_native_unit_of_measurement = "kWh/L"
    _attr_icon = "mdi:flash"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float:
        """Return the kWh per litre conversion factor."""
        return self._coordinator.data.get("kwh_per_litre", DEFAULT_KWH_PER_LITRE)


class BoilerJuiceCostPerKwhSensor(BoilerJuiceSensor):
    """Representation of a BoilerJuice cost per kWh sensor."""

    _attr_name = "Oil Cost Per kWh"
    _attr_native_unit_of_measurement = "GBP/kWh"
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

        cost_per_kwh = (oil_price / kwh_per_litre) / 100  # Convert pence to GBP
        return round(cost_per_kwh, 4)


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
    _attr_native_unit_of_measurement = "L/day"
    _attr_icon = "mdi:weather-partly-cloudy"

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
            "current_season": seasonal_stats.get("current_season", {}).get(
                "name", "unknown"
            ),
            "current_season_min": seasonal_stats.get("current_season", {}).get(
                "min", 0
            ),
            "current_season_max": seasonal_stats.get("current_season", {}).get(
                "max", 0
            ),
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
