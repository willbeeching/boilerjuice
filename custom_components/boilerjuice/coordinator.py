"""Data update coordinator for BoilerJuice."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Union, Dict, Any
import json

from bs4 import BeautifulSoup
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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
        self._total_consumption_usable_liters = 0.0
        self._total_consumption_usable_kwh = 0.0
        self._daily_consumption_usable_liters = 0.0
        self._last_update = None
        self._kwh_per_litre = self._get_config_value_optional(CONF_KWH_PER_LITRE, DEFAULT_KWH_PER_LITRE)

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

    def reset_consumption(self) -> None:
        """Reset the consumption counter."""
        self._total_consumption_usable_liters = 0.0
        self._total_consumption_usable_kwh = 0.0
        self._daily_consumption_usable_liters = 0.0
        self._previous_usable_volume = None
        self._last_update = None

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
                now = datetime.now()

                if self._previous_usable_volume is not None and current_usable_volume < self._previous_usable_volume:
                    liters_used = self._previous_usable_volume - current_usable_volume
                    self._total_consumption_usable_liters += liters_used
                    self._total_consumption_usable_kwh += liters_used * LITERS_TO_KWH

                    # Calculate daily consumption if we have a previous update
                    if self._last_update:
                        days_since_last_update = (now - self._last_update).total_seconds() / 86400
                        if days_since_last_update > 0:
                            self._daily_consumption_usable_liters = liters_used / days_since_last_update

                self._previous_usable_volume = current_usable_volume
                self._last_update = now

                # Add consumption data to tank data
                data["total_consumption_usable_liters"] = self._total_consumption_usable_liters
                data["total_consumption_usable_kwh"] = self._total_consumption_usable_kwh
                data["daily_consumption_usable_liters"] = self._daily_consumption_usable_liters
                data["days_until_empty"] = self._calculate_days_until_empty(data)

                _LOGGER.debug(
                    "Tank data: usable_volume=%s L, daily_consumption=%s L/day, days_until_empty=%s",
                    data.get("usable_volume_litres"),
                    data.get("daily_consumption_usable_liters"),
                    data.get("days_until_empty")
                )

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

                return data

        except Exception as err:
            _LOGGER.exception("Error in _async_update_data: %s", str(err))
            raise