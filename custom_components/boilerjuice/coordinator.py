"""Data update coordinator for BoilerJuice."""
from __future__ import annotations

import logging
import re
import os
import json
from datetime import datetime, timedelta
from typing import Union, Dict, Any

from bs4 import BeautifulSoup
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TANK_ID,
    CONF_KWH_PER_LITRE,
    DEFAULT_KWH_PER_LITRE,
    DOMAIN,
    LOGIN_URL,
    TANKS_URL,
    ACCOUNT_URL,
)

_LOGGER = logging.getLogger(__name__)

# Update once per day
SCAN_INTERVAL = timedelta(days=1)

# Conversion factors
# 1 liter of heating oil = 10.35 kWh (typical value for heating oil)
LITERS_TO_KWH = 10.35

# Storage constants
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_consumption_data"

class BoilerJuiceDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching BoilerJuice data."""

    def __init__(self, hass: HomeAssistant, config: Union[ConfigEntry, Dict[str, Any]]) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self._config = config
        self._session = None
        self._previous_usable_volume = None
        self._previous_total_level = None
        self._total_consumption_usable_liters = 0.0
        self._total_consumption_usable_kwh = 0.0
        self._daily_consumption_usable_liters = 0.0
        self._last_update = None
        self._kwh_per_litre = self._get_config_value_optional(CONF_KWH_PER_LITRE, DEFAULT_KWH_PER_LITRE)

        # Set up storage
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._tank_id = self._get_config_value_optional(CONF_TANK_ID)

        # Load consumption data from storage
        hass.async_create_task(self._load_consumption_data())

    def _get_config_value(self, key: str) -> Any:
        """Get a configuration value, handling both ConfigEntry and dict inputs."""
        if isinstance(self._config, ConfigEntry):
            return self._config.data[key]
        return self._config[key]

    def _get_config_value_optional(self, key: str, default: Any = None) -> Any:
        """Get an optional configuration value, handling both ConfigEntry and dict inputs."""
        if isinstance(self._config, ConfigEntry):
            return self._config.data.get(key, default)
        return self._config.get(key, default)

    @property
    def total_consumption_usable_liters(self) -> float:
        """Return the total oil consumption in liters."""
        return self._total_consumption_usable_liters

    @property
    def total_consumption_usable_kwh(self) -> float:
        """Return the total oil consumption in kWh."""
        return self._total_consumption_usable_kwh

    @property
    def daily_consumption_usable_liters(self) -> float:
        """Return the average daily oil consumption in liters."""
        return self._daily_consumption_usable_liters

    @property
    def days_until_empty(self) -> float | None:
        """Return the estimated days until the tank is empty."""
        if not self.data:
            return None

        current_volume = self.data.get("current_volume_litres")
        if current_volume is None:
            return None

        # If we have actual consumption data, use it
        if self._daily_consumption_usable_liters and self._daily_consumption_usable_liters > 0:
            return current_volume / self._daily_consumption_usable_liters

        # Otherwise, estimate based on current level and capacity
        capacity = self.data.get("capacity_litres")
        level = self.data.get("level_percentage")

        if capacity and level is not None and level > 0:
            # Assume average daily consumption of 2% of tank capacity
            estimated_daily_consumption = capacity * 0.02
            return current_volume / estimated_daily_consumption

        return None

    async def _load_consumption_data(self) -> None:
        """Load consumption data from storage."""
        stored_data = await self._store.async_load()

        if stored_data:
            _LOGGER.debug("Loading stored consumption data: %s", stored_data)

            # If we have a tank ID, try to get data specific to this tank
            if self._tank_id and self._tank_id in stored_data:
                tank_data = stored_data[self._tank_id]

                self._total_consumption_usable_liters = tank_data.get("total_consumption_liters", 0.0)
                self._total_consumption_usable_kwh = tank_data.get("total_consumption_kwh", 0.0)
                self._daily_consumption_usable_liters = tank_data.get("daily_consumption_liters", 0.0)

                # Convert stored string timestamp to datetime if exists
                last_update_str = tank_data.get("last_update")
                if last_update_str:
                    try:
                        self._last_update = datetime.fromisoformat(last_update_str)
                    except (ValueError, TypeError):
                        self._last_update = None

                # Get reference values if available
                self._previous_usable_volume = tank_data.get("reference_volume")
                self._previous_total_level = tank_data.get("reference_level")

                _LOGGER.info(
                    "Loaded stored consumption data for tank %s: total=%s L, daily=%s L/day",
                    self._tank_id,
                    self._total_consumption_usable_liters,
                    self._daily_consumption_usable_liters
                )
            elif not self._tank_id and stored_data.get("default"):
                # Fallback to default if no tank ID
                default_data = stored_data["default"]

                self._total_consumption_usable_liters = default_data.get("total_consumption_liters", 0.0)
                self._total_consumption_usable_kwh = default_data.get("total_consumption_kwh", 0.0)
                self._daily_consumption_usable_liters = default_data.get("daily_consumption_liters", 0.0)

                # Convert stored string timestamp to datetime if exists
                last_update_str = default_data.get("last_update")
                if last_update_str:
                    try:
                        self._last_update = datetime.fromisoformat(last_update_str)
                    except (ValueError, TypeError):
                        self._last_update = None

                # Get reference values if available
                self._previous_usable_volume = default_data.get("reference_volume")
                self._previous_total_level = default_data.get("reference_level")

                _LOGGER.info(
                    "Loaded default stored consumption data: total=%s L, daily=%s L/day",
                    self._total_consumption_usable_liters,
                    self._daily_consumption_usable_liters
                )

    async def _save_consumption_data(self) -> None:
        """Save consumption data to storage."""
        tank_id = self.data.get("id") if self.data else self._tank_id

        if not tank_id:
            tank_id = "default"

        # Load existing data first
        stored_data = await self._store.async_load() or {}

        # Update with current values
        tank_data = {
            "total_consumption_liters": self._total_consumption_usable_liters,
            "total_consumption_kwh": self._total_consumption_usable_kwh,
            "daily_consumption_liters": self._daily_consumption_usable_liters,
            "reference_volume": self._previous_usable_volume,
            "reference_level": self._previous_total_level,
        }

        # Store last update time as ISO format string
        if self._last_update:
            tank_data["last_update"] = self._last_update.isoformat()

        stored_data[tank_id] = tank_data

        # Save to storage
        await self._store.async_save(stored_data)
        _LOGGER.debug("Saved consumption data for tank %s: %s", tank_id, tank_data)

    def reset_consumption(self) -> None:
        """Reset the consumption counter."""
        self._total_consumption_usable_liters = 0.0
        self._total_consumption_usable_kwh = 0.0
        self._daily_consumption_usable_liters = 0.0
        self._previous_usable_volume = None
        self._previous_total_level = None
        self._last_update = None

        # Save the reset to storage
        self.hass.async_create_task(self._save_consumption_data())

    def force_consumption_reference(self, data: dict) -> None:
        """Set the current levels as reference points without resetting consumption stats."""
        current_usable_volume = float(data.get("usable_volume_litres", 0))
        current_total_level = float(data.get("total_level_percentage", 0))

        self._previous_usable_volume = current_usable_volume
        self._previous_total_level = current_total_level
        self._last_update = datetime.now()

        _LOGGER.info(
            "Force-set reference values: usable_volume=%s L, total_level=%s%%",
            current_usable_volume,
            current_total_level
        )

        # Save the new reference values
        self.hass.async_create_task(self._save_consumption_data())

    async def _get_tank_id(self) -> str | None:
        """Get the tank ID from the tanks page."""
        _LOGGER.debug("Accessing tanks page to find tank ID...")
        async with self._session.get(TANKS_URL) as response:
            if response.status != 200:
                _LOGGER.error("Failed to access tanks page with status %s", response.status)
                return None

            text = await response.text()
            soup = BeautifulSoup(text, 'html.parser')
            tank_links = soup.find_all('a', href=re.compile(r'/uk/users/tanks/\d+'))

            if not tank_links:
                _LOGGER.error("Could not find any tank links on the tanks page")
                return None

            tank_id = re.search(r'/uk/users/tanks/(\d+)', tank_links[0]['href']).group(1)
            _LOGGER.debug("Found tank ID: %s", tank_id)
            return tank_id

    def _calculate_days_until_empty(self, data: dict[str, Any]) -> float | None:
        """Calculate the estimated days until empty."""
        current_volume = data.get("current_volume_litres")
        if current_volume is None:
            return None

        # If we have actual consumption data, use it
        if self._daily_consumption_usable_liters and self._daily_consumption_usable_liters > 0:
            return round(current_volume / self._daily_consumption_usable_liters, 1)

        # Otherwise, estimate based on current level and capacity
        capacity = data.get("capacity_litres")
        level = data.get("level_percentage")

        if capacity and level is not None and level > 0:
            # Assume average daily consumption of 2% of tank capacity
            estimated_daily_consumption = capacity * 0.02
            return round(current_volume / estimated_daily_consumption, 1)

        return None

    async def _get_oil_price(self) -> float:
        """Get the current oil price from the kerosene prices page."""
        try:
            response = await self._session.get("https://www.boilerjuice.com/kerosene-prices/")
            response.raise_for_status()
            content = await response.text()

            # Look for the price in the format "XX.XX pence per litre"
            import re
            price_match = re.search(r"(\d+\.\d+)\s*pence per litre", content)
            if price_match:
                return float(price_match.group(1))

            _LOGGER.warning("Could not find current oil price on kerosene prices page")
            return None

        except Exception as e:
            _LOGGER.error(f"Error getting oil price: {e}")
            return None

    async def _async_update_data(self):
        """Fetch data from BoilerJuice."""
        if self._session is None:
            self._session = async_get_clientsession(self.hass)

        try:
            # First, get the login page to get the CSRF token
            _LOGGER.debug("Getting login page for CSRF token...")
            async with self._session.get(LOGIN_URL) as response:
                if response.status != 200:
                    _LOGGER.error("Failed to get login page with status %s", response.status)
                    raise Exception("Failed to get login page")

                text = await response.text()
                soup = BeautifulSoup(text, 'html.parser')
                csrf_token = soup.find('meta', {'name': 'csrf-token'})
                if not csrf_token:
                    _LOGGER.error("Could not find CSRF token")
                    raise Exception("Failed to get CSRF token")

                csrf_token = csrf_token['content']

            # Login to the site
            login_data = {
                "user[email]": self._get_config_value(CONF_EMAIL),
                "user[password]": self._get_config_value(CONF_PASSWORD),
                "authenticity_token": csrf_token,
                "commit": "Sign in",
            }

            _LOGGER.debug("Logging in...")
            async with self._session.post(LOGIN_URL, data=login_data) as response:
                if response.status != 200:
                    _LOGGER.error("Login failed with status %s", response.status)
                    raise Exception("Failed to login to BoilerJuice")

                # Check if we're still on the login page (indicating failed login)
                text = await response.text()
                if "Sign in" in text:
                    _LOGGER.error("Login failed - still on login page")
                    raise Exception("Invalid credentials")

            # Get or find tank ID
            tank_id = self._get_config_value_optional(CONF_TANK_ID)
            if not tank_id:
                tank_id = await self._get_tank_id()
                if not tank_id:
                    raise Exception("Could not find tank ID")

            # Get the tank details page
            tank_url = f"{TANKS_URL}/{tank_id}/edit"
            _LOGGER.debug("Accessing tank page at %s", tank_url)
            async with self._session.get(tank_url) as response:
                if response.status != 200:
                    _LOGGER.error("Failed to get tank page with status %s", response.status)
                    raise Exception("Failed to get tank data from BoilerJuice")

                text = await response.text()
                soup = BeautifulSoup(text, 'html.parser')
                data = {}

                # Get tank level percentage
                total_level_input = soup.find('input', {'name': 'percentage'})
                if total_level_input and total_level_input.get('value'):
                    data["total_level_percentage"] = int(total_level_input['value'])
                    _LOGGER.debug("Found total tank level: %s%%", data["total_level_percentage"])

                # Get usable oil level percentage
                usable_level_div = soup.find("div", {"id": "usable-oil"})
                if usable_level_div:
                    oil_level = usable_level_div.find("div", {"class": "oil-level"})
                    if oil_level and oil_level.get("data-percentage"):
                        data["usable_level_percentage"] = float(oil_level["data-percentage"])
                        _LOGGER.debug("Found usable tank level: %s%%", data["usable_level_percentage"])

                # Get tank size
                tank_size_input = soup.find('input', {'id': 'tank-size-count'})
                if tank_size_input and tank_size_input.get('value'):
                    data["capacity_litres"] = int(tank_size_input['value'])
                    _LOGGER.debug("Found tank capacity: %s litres", data["capacity_litres"])
                else:
                    _LOGGER.debug("Tank size input not found")
                    size_context = soup.find('input', {'name': re.compile(r'.*size.*', re.I)})
                    if size_context:
                        _LOGGER.debug("Found similar size input: %s", size_context)

                # Get tank height
                tank_height_input = soup.find('input', {'id': 'tank-height-count'})
                if tank_height_input and tank_height_input.get('value'):
                    data["height_cm"] = int(tank_height_input['value'])
                    _LOGGER.debug("Found tank height: %s cm", data["height_cm"])
                else:
                    _LOGGER.debug("Tank height input not found")
                    height_context = soup.find('input', {'name': re.compile(r'.*height.*', re.I)})
                    if height_context:
                        _LOGGER.debug("Found similar height input: %s", height_context)

                # Look for volume information in text
                volume_texts = soup.find_all(string=lambda text: text and any(word in text.lower() for word in ['litre', 'volume', 'oil level']))
                for text in volume_texts:
                    text = text.strip()

                    # Extract usable volume
                    if "usable oil" in text.lower():
                        match = re.search(r'(\d+)\s*litres?\s+of\s+usable\s+oil', text.lower())
                        if match:
                            data["usable_volume_litres"] = int(match.group(1))
                            _LOGGER.debug("Found usable volume: %s litres", data["usable_volume_litres"])

                    # Extract total volume
                    elif "litres of oil left" in text.lower():
                        match = re.search(r'(\d+)\s*litres?\s+of\s+oil\s+left', text.lower())
                        if match:
                            data["current_volume_litres"] = int(match.group(1))
                            _LOGGER.debug("Found total volume: %s litres", data["current_volume_litres"])

                # Get tank name
                tank_name_input = soup.find('input', {'id': 'tank_user_tanks_attributes_0_name'})
                if tank_name_input and tank_name_input.get('value'):
                    data["name"] = tank_name_input['value']
                    _LOGGER.debug("Found tank name: %s", data["name"])

                # Get tank manufacturer/model
                tank_model_input = soup.find('input', {'id': 'tankModelInput'})
                if tank_model_input and tank_model_input.get('value'):
                    model_id = tank_model_input.get('value')
                    data["model_id"] = model_id
                    _LOGGER.debug("Found tank model ID: %s", model_id)

                    # Try to find the manufacturer data in the JavaScript
                    scripts = soup.find_all('script')
                    for script in scripts:
                        if script.string and 'var jsonData = ' in script.string:
                            _LOGGER.debug("Found jsonData variable")
                            script_text = script.string
                            start_idx = script_text.find('var jsonData = ')
                            if start_idx >= 0:
                                # Try to find where the JSON array ends
                                array_start = script_text.find('[', start_idx)
                                if array_start >= 0:
                                    bracket_count = 1
                                    array_end = array_start + 1
                                    while array_end < len(script_text) and bracket_count > 0:
                                        if script_text[array_end] == '[':
                                            bracket_count += 1
                                        elif script_text[array_end] == ']':
                                            bracket_count -= 1
                                        array_end += 1

                                    if bracket_count == 0:
                                        json_str = script_text[array_start:array_end]
                                        try:
                                            json_data = json.loads(json_str)
                                            # Find the manufacturer for our model ID
                                            for item in json_data:
                                                if str(item.get('id')) == str(model_id):
                                                    data["model"] = item.get('tank', {}).get('Description')
                                                    data["manufacturer"] = item.get('tank', {}).get('Brand')
                                                    _LOGGER.debug("Found manufacturer from JSON: %s", data["model"])
                                                    break
                                        except json.JSONDecodeError as e:
                                            _LOGGER.error("Failed to parse tank model JSON: %s", e)
                            break
                else:
                    _LOGGER.debug("Could not find tank model ID")

                # Get tank shape
                for shape in ['cuboid', 'horizontal_cylinder', 'vertical_cylinder']:
                    shape_input = soup.find('input', {'type': 'radio', 'name': 'tank-shape', 'value': shape})
                    if shape_input and shape_input.get('checked'):
                        data["shape"] = shape.replace('_', ' ').title()
                        _LOGGER.debug("Found tank shape: %s", data["shape"])
                        break

                # Get oil type
                oil_type_select = soup.find('select', {'id': 'tank_oil_type_id'})
                if oil_type_select:
                    selected_option = oil_type_select.find('option', selected=True)
                    if selected_option:
                        data["oil_type"] = selected_option.text
                        _LOGGER.debug("Found oil type: %s", data["oil_type"])

                # Add tank ID
                data["id"] = tank_id

                if not data:
                    raise Exception("Could not find any tank details")

                # Calculate consumption based on usable oil
                current_usable_volume = float(data.get("usable_volume_litres", 0))
                current_total_level = float(data.get("total_level_percentage", 0))
                now = datetime.now()

                # Log current state
                _LOGGER.debug(
                    "Current state: usable_volume=%s L, total_level=%s%%, previous_volume=%s L, previous_level=%s%%",
                    current_usable_volume,
                    current_total_level,
                    self._previous_usable_volume,
                    self._previous_total_level
                )

                # If we don't have previous values, set them without calculating consumption
                if self._previous_usable_volume is None or self._previous_total_level is None:
                    _LOGGER.info("First update or reference values missing - setting initial values without calculating consumption")
                    self.force_consumption_reference(data)

                    # For manual consumption based on current value
                    # If both usable oil volumes and percentages are valid and seem to indicate consumption, calculate it
                    if data.get("capacity_litres") and current_total_level < 100:
                        capacity = data.get("capacity_litres")
                        # Calculate how much oil has been used (100% - current_level)%
                        estimated_used = ((100 - current_total_level) / 100) * capacity
                        _LOGGER.info(
                            "Estimated consumption based on current level (%s%%): %s L out of %s L capacity",
                            current_total_level,
                            round(estimated_used, 1),
                            capacity
                        )
                else:
                    # Track consumption based on direct volume change if available
                    consumption_detected = False
                    if self._previous_usable_volume is not None and current_usable_volume < self._previous_usable_volume:
                        liters_used = self._previous_usable_volume - current_usable_volume
                        _LOGGER.info(
                            "Detected consumption from volume change: %s L (from %s L to %s L)",
                            liters_used,
                            self._previous_usable_volume,
                            current_usable_volume
                        )

                        self._total_consumption_usable_liters += liters_used
                        self._total_consumption_usable_kwh += liters_used * LITERS_TO_KWH
                        consumption_detected = True

                        # Calculate daily consumption if we have a previous update
                        if self._last_update:
                            days_since_last_update = (now - self._last_update).total_seconds() / 86400
                            if days_since_last_update > 0:
                                self._daily_consumption_usable_liters = liters_used / days_since_last_update
                                _LOGGER.info(
                                    "Updated daily consumption to %s L/day (over %s days)",
                                    self._daily_consumption_usable_liters,
                                    days_since_last_update
                                )

                    # If no consumption detected from volume, check percentage change
                    if not consumption_detected and self._previous_total_level is not None:
                        _LOGGER.debug(
                            "Checking level change: current=%s%%, previous=%s%%",
                            current_total_level,
                            self._previous_total_level
                        )

                        if current_total_level < self._previous_total_level:
                            # Calculate liters based on percentage change
                            capacity = data.get("capacity_litres")
                            if capacity:
                                percent_change = self._previous_total_level - current_total_level
                                liters_used = (percent_change / 100) * capacity
                                _LOGGER.info(
                                    "Detected consumption from level change: %s%% (%s L) - tank capacity: %s L",
                                    percent_change,
                                    liters_used,
                                    capacity
                                )

                                self._total_consumption_usable_liters += liters_used
                                self._total_consumption_usable_kwh += liters_used * LITERS_TO_KWH
                                consumption_detected = True

                                # Calculate daily consumption if we have a previous update
                                if self._last_update:
                                    days_since_last_update = (now - self._last_update).total_seconds() / 86400
                                    if days_since_last_update > 0:
                                        self._daily_consumption_usable_liters = liters_used / days_since_last_update
                                        _LOGGER.info(
                                            "Updated daily consumption to %s L/day (over %s days)",
                                            self._daily_consumption_usable_liters,
                                            days_since_last_update
                                        )

                    # Update previous values regardless of consumption
                    self._previous_usable_volume = current_usable_volume
                    self._previous_total_level = current_total_level
                    self._last_update = now

                # Add consumption data to tank data
                data["total_consumption_usable_liters"] = self._total_consumption_usable_liters
                data["total_consumption_usable_kwh"] = self._total_consumption_usable_kwh
                data["daily_consumption_usable_liters"] = self._daily_consumption_usable_liters
                data["days_until_empty"] = self._calculate_days_until_empty(data)

                _LOGGER.info(
                    "Consumption data: total=%s L, daily=%s L/day, total_kwh=%s",
                    round(self._total_consumption_usable_liters, 1),
                    round(self._daily_consumption_usable_liters, 1),
                    round(self._total_consumption_usable_kwh, 1)
                )

                # Ensure correct kWh calculation (sometimes this might be out of sync)
                if abs(self._total_consumption_usable_kwh - (self._total_consumption_usable_liters * LITERS_TO_KWH)) > 0.1:
                    _LOGGER.info(
                        "Correcting kWh value from %s to %s",
                        self._total_consumption_usable_kwh,
                        self._total_consumption_usable_liters * LITERS_TO_KWH
                    )
                    self._total_consumption_usable_kwh = self._total_consumption_usable_liters * LITERS_TO_KWH
                    data["total_consumption_usable_kwh"] = self._total_consumption_usable_kwh

                # Get current oil price from kerosene prices page
                try:
                    async with self._session.get("https://www.boilerjuice.com/kerosene-prices/") as price_response:
                        if price_response.status == 200:
                            price_text = await price_response.text()
                            price_match = re.search(r"(\d+\.\d+)\s*pence per litre", price_text)
                            if price_match:
                                data["current_price_pence"] = float(price_match.group(1))
                                _LOGGER.debug("Found current oil price: %s pence per litre", data["current_price_pence"])
                except Exception as e:
                    _LOGGER.error("Error getting oil price: %s", e)

                # Add kWh per litre to the data
                data["kwh_per_litre"] = self._kwh_per_litre

                # Save consumption data to storage
                self.hass.async_create_task(self._save_consumption_data())

                return data

        except Exception as err:
            _LOGGER.exception("Error in _async_update_data: %s", str(err))
            raise