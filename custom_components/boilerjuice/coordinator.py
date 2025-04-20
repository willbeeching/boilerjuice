"""Data update coordinator for BoilerJuice."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Union, Dict, Any

from bs4 import BeautifulSoup
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TANK_ID,
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
        self._previous_volume = None
        self._total_consumption_liters = 0.0
        self._total_consumption_kwh = 0.0
        self._daily_consumption_liters = 0.0
        self._last_update = None

    def _get_config_value(self, key: str) -> Any:
        """Get a configuration value, handling both ConfigEntry and dict inputs."""
        if isinstance(self._config, ConfigEntry):
            return self._config.data[key]
        return self._config[key]

    def _get_config_value_optional(self, key: str) -> Any:
        """Get an optional configuration value, handling both ConfigEntry and dict inputs."""
        if isinstance(self._config, ConfigEntry):
            return self._config.data.get(key)
        return self._config.get(key)

    @property
    def total_consumption_liters(self) -> float:
        """Return the total oil consumption in liters."""
        return self._total_consumption_liters

    @property
    def total_consumption_kwh(self) -> float:
        """Return the total oil consumption in kWh."""
        return self._total_consumption_kwh

    @property
    def daily_consumption_liters(self) -> float:
        """Return the average daily oil consumption in liters."""
        return self._daily_consumption_liters

    @property
    def days_until_empty(self) -> float | None:
        """Return the estimated days until the tank is empty."""
        if not self.data:
            return None

        current_volume = self.data.get("current_volume_litres")
        if current_volume is None:
            return None

        # If we have actual consumption data, use it
        if self._daily_consumption_liters and self._daily_consumption_liters > 0:
            return current_volume / self._daily_consumption_liters

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
        self._total_consumption_liters = 0.0
        self._total_consumption_kwh = 0.0
        self._daily_consumption_liters = 0.0
        self._previous_volume = None
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
        if self._daily_consumption_liters and self._daily_consumption_liters > 0:
            return round(current_volume / self._daily_consumption_liters, 1)

        # Otherwise, estimate based on current level and capacity
        capacity = data.get("capacity_litres")
        level = data.get("level_percentage")

        if capacity and level is not None and level > 0:
            # Assume average daily consumption of 2% of tank capacity
            estimated_daily_consumption = capacity * 0.02
            return round(current_volume / estimated_daily_consumption, 1)

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
                percentage_input = soup.find('input', {'name': 'percentage'})
                if percentage_input and percentage_input.get('value'):
                    data["level_percentage"] = int(percentage_input['value'])
                    _LOGGER.debug("Found tank level: %s%%", data["level_percentage"])

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

                # Get current oil volume estimate
                volume_text = soup.find('p', string=re.compile(r'.*litres of.*tank.*'))
                if volume_text:
                    volume_match = re.search(r'(\d+)\s*litres', volume_text.text)
                    if volume_match:
                        data["current_volume_litres"] = int(volume_match.group(1))
                        _LOGGER.debug("Found current volume: %s litres", data["current_volume_litres"])

                # Get tank shape
                for shape in ["cuboid", "horizontal_cylinder", "vertical_cylinder"]:
                    shape_input = soup.find('input', {
                        'type': 'radio',
                        'name': 'tank-shape',
                        'value': shape,
                        'checked': True
                    })
                    if shape_input:
                        data["shape"] = shape.replace("_", " ").title()
                        _LOGGER.debug("Found tank shape: %s", data["shape"])
                        break

                # Get oil type
                oil_type_select = soup.find('select', {'id': 'tank_oil_type_id'})
                if oil_type_select:
                    selected_option = oil_type_select.find('option', selected=True)
                    if selected_option:
                        data["oil_type"] = selected_option.text.strip()
                        _LOGGER.debug("Found oil type: %s", data["oil_type"])

                # Get tank name
                name_input = soup.find('input', {'id': 'tank_user_tanks_attributes_0_name'})
                if name_input and name_input.get('value'):
                    data["name"] = name_input['value']
                    _LOGGER.debug("Found tank name: %s", data["name"])

                # Get tank model
                model_input = soup.find('input', {'id': 'tank_model'})
                if model_input and model_input.get('value'):
                    data["model"] = model_input['value']
                    _LOGGER.debug("Found tank model: %s", data["model"])

                # Add tank ID
                data["id"] = tank_id

                if not data:
                    raise Exception("Could not find any tank details")

                # Calculate consumption
                current_volume = float(data.get("current_volume_litres", 0))
                now = datetime.now()

                if self._previous_volume is not None and current_volume < self._previous_volume:
                    liters_used = self._previous_volume - current_volume
                    self._total_consumption_liters += liters_used
                    self._total_consumption_kwh += liters_used * LITERS_TO_KWH

                    # Calculate daily consumption if we have a previous update
                    if self._last_update:
                        days_since_last_update = (now - self._last_update).total_seconds() / 86400
                        if days_since_last_update > 0:
                            self._daily_consumption_liters = liters_used / days_since_last_update

                self._previous_volume = current_volume
                self._last_update = now

                # Add consumption data to tank data
                data["total_consumption_liters"] = self._total_consumption_liters
                data["total_consumption_kwh"] = self._total_consumption_kwh
                data["daily_consumption_liters"] = self._daily_consumption_liters
                data["days_until_empty"] = self._calculate_days_until_empty(data)

                _LOGGER.debug(
                    "Tank data: volume=%s L, capacity=%s L, daily_consumption=%s L/day, days_until_empty=%s",
                    data.get("current_volume_litres"),
                    data.get("capacity_litres"),
                    data.get("daily_consumption_liters"),
                    data.get("days_until_empty")
                )

                return data

        except Exception as err:
            _LOGGER.exception("Error in _async_update_data: %s", str(err))
            raise