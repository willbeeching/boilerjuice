"""Sensor platform for BoilerJuice.

One set of entities per tank, described declaratively. Names come from
translation keys rather than hard-coded English, and unique ids from stable
keys rather than the Python class name, so renaming a class no longer
renames a user's entity.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_KWH_PER_LITRE, DOMAIN
from .coordinator import BoilerJuiceDataUpdateCoordinator
from .runtime import BoilerJuiceConfigEntry

_LOGGER = logging.getLogger(__name__)

# Unit strings Home Assistant has no constant for.
LITRES_PER_DAY = "L/day"
KWH_PER_LITRE = "kWh/L"
GBP_PER_LITRE = "GBP/L"
GBP_PER_KWH = "GBP/kWh"
DAYS = "d"


def _rounded(key: str, places: int = 1) -> Callable[[dict[str, Any]], StateType]:
    """Return a reader that rounds one published field."""

    def read(data: dict[str, Any]) -> StateType:
        value = data.get(key)
        return None if value is None else round(value, places)

    return read


def _cost_per_kwh(data: dict[str, Any]) -> StateType:
    """Return the cost of a kWh of heat, in pounds."""
    price: float | None = data.get("current_price_pence")
    energy_content: float | None = data.get("kwh_per_litre")
    if price is None or not energy_content:
        return None
    return round((price / energy_content) / 100, 4)


def _oil_price(data: dict[str, Any]) -> StateType:
    """Return the oil price in pounds per litre."""
    price: float | None = data.get("current_price_pence")
    return None if price is None else round(price / 100, 2)


def _current_season(data: dict[str, Any]) -> StateType:
    """Return this season's average daily consumption."""
    average: float | None = (
        data.get("seasonal_stats", {}).get("current_season", {}).get("avg")
    )
    return average


def _price_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return the price sensor's attributes."""
    return {
        "price_pence_per_litre": data.get("current_price_pence"),
        "last_updated": data.get("price_last_updated"),
    }


def _daily_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Say how much evidence is behind the daily rate."""
    return {
        "sample_days": data.get("consumption_sample_days", 0),
        "manually_set": data.get("daily_consumption_is_manual", False),
    }


def _seasonal_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return the seasonal breakdown.

    A season we have no data for is unknown, not a tank that burnt nothing
    all winter.
    """
    stats = data.get("seasonal_stats") or {}
    if not stats:
        return {}

    current = stats.get("current_season", {})
    attributes: dict[str, Any] = {
        "current_season": current.get("name") or None,
        "current_season_min": current.get("min"),
        "current_season_max": current.get("max"),
        "winter_average": stats.get("winter_avg"),
        "spring_average": stats.get("spring_avg"),
        "summer_average": stats.get("summer_avg"),
        "autumn_average": stats.get("autumn_avg"),
    }
    if stats.get("monthly"):
        attributes["monthly_averages"] = stats["monthly"]
    return attributes


@dataclass(frozen=True, kw_only=True)
class BoilerJuiceSensorDescription(SensorEntityDescription):
    """Describes one BoilerJuice sensor."""

    value: Callable[[dict[str, Any]], StateType | datetime]
    attributes: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # The class name this sensor's unique id used to be built from, so
    # existing entities keep their identity across the upgrade.
    legacy_class: str | None = None


SENSORS: tuple[BoilerJuiceSensorDescription, ...] = (
    BoilerJuiceSensorDescription(
        key="oil_level",
        translation_key="oil_level",
        # Not a battery: a tank is not a cell, and the battery device class
        # puts it in low-battery alerts and battery dashboards.
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:storage-tank",
        value=lambda data: data.get("total_level_percentage"),
        legacy_class="BoilerJuiceOilLevelSensor",
    ),
    BoilerJuiceSensorDescription(
        key="tank_volume",
        translation_key="tank_volume",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda data: data.get("current_volume_litres"),
        legacy_class="BoilerJuiceTankVolumeSensor",
    ),
    BoilerJuiceSensorDescription(
        key="tank_capacity",
        translation_key="tank_capacity",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.VOLUME,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda data: data.get("capacity_litres"),
        legacy_class="BoilerJuiceTankCapacitySensor",
    ),
    BoilerJuiceSensorDescription(
        key="tank_height",
        translation_key="tank_height",
        native_unit_of_measurement=UnitOfLength.CENTIMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda data: data.get("height_cm"),
        legacy_class="BoilerJuiceTankHeightSensor",
    ),
    BoilerJuiceSensorDescription(
        key="daily_consumption",
        translation_key="daily_consumption",
        native_unit_of_measurement=LITRES_PER_DAY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gauge",
        value=_rounded("daily_consumption_usable_liters"),
        attributes=_daily_attributes,
        legacy_class="BoilerJuiceDailyConsumptionSensor",
    ),
    BoilerJuiceSensorDescription(
        key="total_consumption",
        translation_key="total_consumption",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=_rounded("total_consumption_usable_liters"),
        legacy_class="BoilerJuiceTotalConsumptionSensor",
    ),
    BoilerJuiceSensorDescription(
        key="total_consumption_kwh",
        translation_key="total_consumption_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=_rounded("total_consumption_usable_kwh"),
        legacy_class="BoilerJuiceTotalConsumptionKwhSensor",
    ),
    BoilerJuiceSensorDescription(
        key="days_until_empty",
        translation_key="days_until_empty",
        native_unit_of_measurement=DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar-clock",
        value=lambda data: data.get("days_until_empty"),
        legacy_class="BoilerJuiceDaysUntilEmptySensor",
    ),
    BoilerJuiceSensorDescription(
        key="energy_content",
        translation_key="energy_content",
        native_unit_of_measurement=KWH_PER_LITRE,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:flash",
        value=lambda data: data.get("kwh_per_litre", DEFAULT_KWH_PER_LITRE),
        legacy_class="BoilerJuiceKwhPerLitreSensor",
    ),
    BoilerJuiceSensorDescription(
        key="cost_per_kwh",
        translation_key="cost_per_kwh",
        native_unit_of_measurement=GBP_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:currency-gbp",
        value=_cost_per_kwh,
        legacy_class="BoilerJuiceCostPerKwhSensor",
    ),
    BoilerJuiceSensorDescription(
        key="oil_price",
        translation_key="oil_price",
        native_unit_of_measurement=GBP_PER_LITRE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:currency-gbp",
        value=_oil_price,
        attributes=_price_attributes,
        legacy_class="BoilerJuiceOilPriceSensor",
    ),
    BoilerJuiceSensorDescription(
        key="last_level_change",
        # Renamed from "Last Updated", which read as "when we last polled".
        # It has always meant "when the level was last seen to change".
        translation_key="last_level_change",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda data: data.get("last_level_change"),
        legacy_class="BoilerJuiceLastUpdateSensor",
    ),
    BoilerJuiceSensorDescription(
        key="last_successful_update",
        translation_key="last_successful_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda data: data.get("last_successful_update"),
    ),
    BoilerJuiceSensorDescription(
        key="seasonal_consumption",
        translation_key="seasonal_consumption",
        native_unit_of_measurement=LITRES_PER_DAY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-partly-cloudy",
        value=_current_season,
        attributes=_seasonal_attributes,
        legacy_class="BoilerJuiceSeasonalConsumptionSensor",
    ),
)

# The incremental "Oil Consumption (kWh)" sensor accumulated state as a side
# effect of being read, which made its value depend on how often something
# looked at it. Its cumulative sibling is the correct Energy dashboard
# source, so the incremental one is gone and its registry entry goes with it
# rather than lingering as a permanently unavailable entity.
RETIRED_UNIQUE_ID_SUFFIXES = ("_BoilerJuiceIncrementalConsumptionKwhSensor",)


@callback
def _async_tidy_registry(hass: HomeAssistant, tank_id: str) -> None:
    """Retire removed entities and move surviving ones onto stable ids."""
    registry = er.async_get(hass)

    for suffix in RETIRED_UNIQUE_ID_SUFFIXES:
        entity_id = registry.async_get_entity_id(
            SENSOR_DOMAIN, DOMAIN, f"{tank_id}{suffix}"
        )
        if entity_id is not None:
            _LOGGER.info("Removing the retired %s entity", entity_id)
            registry.async_remove(entity_id)

    for description in SENSORS:
        if description.legacy_class is None:
            continue
        legacy_id = f"{tank_id}_{description.legacy_class}"
        entity_id = registry.async_get_entity_id(SENSOR_DOMAIN, DOMAIN, legacy_id)
        if entity_id is None:
            continue
        new_id = f"{tank_id}_{description.key}"
        if registry.async_get_entity_id(SENSOR_DOMAIN, DOMAIN, new_id) is not None:
            # Already migrated; the stale row would collide.
            continue
        _LOGGER.debug("Migrating %s onto a stable unique id", entity_id)
        registry.async_update_entity(entity_id, new_unique_id=new_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoilerJuiceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BoilerJuice sensors for every tank on the account."""
    coordinator = entry.runtime_data.coordinator

    def build(tank_ids: list[str]) -> list[BoilerJuiceSensor]:
        entities: list[BoilerJuiceSensor] = []
        for tank_id in tank_ids:
            _async_tidy_registry(hass, tank_id)
            entities.extend(
                BoilerJuiceSensor(coordinator, tank_id, description)
                for description in SENSORS
            )
        return entities

    async_add_entities(build(coordinator.tank_ids))

    @callback
    def _async_add_new_tanks(tank_ids: list[str]) -> None:
        """Add entities for tanks that appeared after setup."""
        async_add_entities(build(tank_ids))

    coordinator.async_add_new_tank_listener(_async_add_new_tanks)


class BoilerJuiceSensor(
    CoordinatorEntity[BoilerJuiceDataUpdateCoordinator], SensorEntity
):
    """One reading of one tank."""

    _attr_has_entity_name = True
    entity_description: BoilerJuiceSensorDescription

    def __init__(
        self,
        coordinator: BoilerJuiceDataUpdateCoordinator,
        tank_id: str,
        description: BoilerJuiceSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._tank_id = tank_id
        self.entity_description = description
        self._attr_unique_id = f"{tank_id}_{description.key}"
        self._attr_device_info = coordinator.device_info(tank_id)

    @property
    def data(self) -> dict[str, Any] | None:
        """Return this tank's published reading, if there is one."""
        return self.coordinator.reading(self._tank_id)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self.data is not None

    @property
    def native_value(self) -> StateType | datetime:
        """Return the state of the sensor."""
        data = self.data
        return None if data is None else self.entity_description.value(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the sensor's attributes, if it has any."""
        data = self.data
        if data is None or self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(data)
