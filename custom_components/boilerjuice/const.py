"""Constants for the BoilerJuice integration."""
from __future__ import annotations
from datetime import timedelta

DOMAIN = "boilerjuice"
NAME = "BoilerJuice"

# Configuration
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_TANK_ID = "tank_id"
CONF_KWH_PER_LITRE = "kwh_per_litre"

# Default Values
DEFAULT_KWH_PER_LITRE = 10.35  # typical value for heating oil

# URLs
BASE_URL = "https://www.boilerjuice.com/uk"
LOGIN_URL = f"{BASE_URL}/users/login"
TANKS_URL = f"{BASE_URL}/users/tanks"
ACCOUNT_URL = f"{BASE_URL}/users/account"

# Defaults
DEFAULT_SCAN_INTERVAL = 300  # 5 minutes

# Sensors
SENSOR_LEVEL = "Oil Tank Level"
SENSOR_VOLUME = "Oil Tank Volume"
SENSOR_CAPACITY = "Oil Tank Capacity"
SENSOR_HEIGHT = "Oil Tank Height"

# Units
UNIT_PERCENTAGE = "%"
UNIT_LITRES = "L"
UNIT_CM = "cm"

# Attributes
ATTR_TANK_NAME = "tank_name"
ATTR_TANK_SHAPE = "tank_shape"
ATTR_OIL_TYPE = "oil_type"
ATTR_TANK_MODEL = "tank_model"
ATTR_TANK_ID = "tank_id"