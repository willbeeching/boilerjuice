# BoilerJuice Integration for Home Assistant

[![CI](https://github.com/willbeeching/boilerjuice/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/boilerjuice/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/boilerjuice)](https://github.com/willbeeching/boilerjuice/releases)
[![License](https://img.shields.io/github/license/willbeeching/boilerjuice)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/hacs/integration)

This custom integration allows you to monitor your BoilerJuice oil tank details in Home Assistant.

## ✨ Features

- 📊 Monitor your oil tank level and volume
- 📈 Track oil consumption with accurate time-based calculations
- 💰 View current oil prices
- ⚡ Calculate energy costs (kWh)
- ⏱️ Estimate days until empty
- 🌡️ Seasonal consumption tracking (Winter/Spring/Summer/Autumn)
- 📉 7-day rolling average for consumption
- 🔔 Refill detection

## How It Works

The integration works by:

1. **Data Collection**:

   - Logs into your BoilerJuice account securely
   - Scrapes tank data from your account page
   - Fetches current oil prices from the kerosene prices page
   - Updates once per day to avoid excessive requests

2. **Tank Monitoring**:

   - Tracks oil level percentage
   - Monitors current volume and tank capacity
   - Records tank dimensions (height, capacity)
   - **Note**: BoilerJuice simplified their interface and now provides a single oil level (previously had separate total/usable)

3. **Consumption Tracking** (Time-Based):

   - **Smart tracking**: Only updates reference values when tank level actually changes
   - **Accurate daily rate**: Spreads consumption across actual days between BoilerJuice updates
   - **Refill detection**: Automatically detects refills and resets reference without losing history
   - **Rolling average**: 7-day rolling average for smoother consumption trends
   - **Seasonal analysis**: Tracks consumption patterns by season (Winter/Spring/Summer/Autumn)
   - Converts oil consumption to energy (kWh)
   - Estimates days until empty based on actual usage patterns

4. **Energy Calculations**:

   - Converts oil volume to energy using configurable kWh/L value
   - Default energy content is 10.35 kWh/L for heating oil
   - Calculates cost per kWh based on current oil price
   - Helps compare heating costs with other energy sources

5. **Data Updates**:
   - Automatically refreshes data daily
   - Updates all sensors simultaneously
   - Maintains historical consumption data
   - Allows manual reset of consumption counters

## Installation

1. Copy the `custom_components/boilerjuice` folder to your Home Assistant `custom_components` directory
2. Restart Home Assistant
3. Go to Configuration > Integrations
4. Click the "+ ADD INTEGRATION" button
5. Search for "BoilerJuice" and select it
6. Enter your BoilerJuice email and password
7. Optionally configure the kWh per litre value (defaults to 10.35 kWh/L for heating oil)

## Configuration

### Required Configuration

- **Email**: Your BoilerJuice account email address
- **Password**: Your BoilerJuice account password

### Optional Configuration

- **Tank ID**: Your tank ID if you have multiple tanks (auto-detected if not specified)
- **kWh per litre**: Energy content of your oil in kWh per litre (default: 10.35 for heating oil)

## Available Sensors

### Tank Levels

- **Oil Level** (%) - Current oil level as a percentage of tank capacity
  - Device Class: Battery (shows as battery icon in UI)
  - Shows the single oil level provided by BoilerJuice

### Volumes

- **Tank Volume** (L) - Current volume of oil in the tank
- **Tank Capacity** (L) - Total tank capacity

### Consumption

- **Daily Oil Consumption** (L/day) - Average daily oil consumption
  - Calculated by spreading consumption across actual days between level changes
  - Uses 7-day rolling average for accurate trending
- **Total Oil Consumption** (L) - Total oil consumed since last reset
  - Accumulates whenever tank level decreases
- **Total Oil Consumption (kWh)** - Total energy consumed
  - Converted from litres using kWh/L ratio
- **Oil Consumption (kWh)** - Incremental energy consumption sensor
  - For use with Home Assistant Energy dashboard
- **Seasonal Oil Consumption** (L/day) - Current season's average daily consumption
  - Tracks patterns across Winter, Spring, Summer, and Autumn

### Cost and Energy

- **BoilerJuice Oil Price** (GBP/litre) - Current oil price per litre
- **Oil Energy Content** (kWh/L) - Energy content of your oil
- **Oil Cost per kWh** (GBP/kWh) - Current cost of energy from your oil

### Other

- **Days Until Empty** (days) - Estimated days until tank is empty based on current consumption rate
- **Tank Height** (cm) - Physical height of your tank
- **Last Updated** - Timestamp of when tank level last changed (not when integration last ran)

## Services

### Reset Consumption

Resets the consumption counters to zero and sets current level as new baseline.

**Use this after upgrading from older versions to clear stuck reference values.**

```yaml
service: boilerjuice.reset_consumption
```

### Set Consumption

Manually set consumption values (useful for initializing with known values).

```yaml
service: boilerjuice.set_consumption
data:
  liters: 500 # Total litres consumed
  daily: 15 # Optional: daily consumption rate in L/day
```

## Migration from v1.0.x

If you're upgrading from v1.0.x or earlier, please note:

### Breaking Changes

1. **Simplified Sensors**: Duplicate sensors have been removed

   - `Total Oil Level` and `Usable Oil Level` → now just `Oil Level`
   - `Usable Oil Volume` → now just `Tank Volume`

2. **Update Your Dashboards**: Replace old sensor entities with new ones
   - `sensor.my_tank_total_oil_level` → `sensor.my_tank_oil_level`
   - `sensor.my_tank_usable_oil_level` → `sensor.my_tank_oil_level`
   - `sensor.my_tank_usable_oil_volume` → `sensor.my_tank_tank_volume`

### Required Actions After Upgrade

1. **Reset consumption tracking** to clear stuck reference values:

   ```yaml
   service: boilerjuice.reset_consumption
   ```

2. **Update automations and dashboards** that reference old sensor entities

3. Old sensor entities will become `unavailable` - you can safely remove them from the entity registry

## Development

### Setup Development Environment

1. Clone this repository
2. Create a virtual environment: `python3 -m venv venv`
3. Activate the virtual environment: `source venv/bin/activate`
4. Install dependencies: `pip install -r requirements.txt`

### Running Tests

```bash
python3 test_boilerjuice.py
```

### Environment Variables

Create a `.env` file with your BoilerJuice credentials:

```bash
BOILERJUICE_EMAIL=your_email@example.com
BOILERJUICE_PASSWORD=your_password
```

## How Consumption Tracking Works

### Time-Based Calculation

The integration uses smart time-based consumption tracking:

```
Example:
Dec 1: Tank at 850L → Reference saved
Dec 2-5: BoilerJuice data unchanged (850L) → Reference stays at 850L
Dec 6: Tank at 800L → Detected 50L used over 5 days = 10 L/day ✅

Old behavior would have shown: 50 L/day (dividing by 1 day) ❌
```

### Key Features

1. **Only updates when level changes**: Reference values only update when BoilerJuice reports a different tank level
2. **Spreads across actual time**: Consumption is divided by days since last level change
3. **Refill detection**: Automatically detects when tank level goes up (refill)
4. **Rolling average**: Maintains 7-day rolling average for smoother trends
5. **Seasonal tracking**: Analyzes consumption patterns by season

### Understanding the Sensors

- **Last Updated**: Shows when tank level last **changed**, not when integration last ran
- **Daily Consumption**: Rolling 7-day average of consumption spread across actual days
- **Days Until Empty**: Current volume ÷ daily consumption rate

## Troubleshooting

### Common Issues

- **Authentication errors**: Double-check your email and password
- **Missing data**: Ensure your BoilerJuice account is active and has a tank configured
- **Incorrect readings**: Verify your tank details on the BoilerJuice website
- **Consumption stuck at 0**: Run the `boilerjuice.reset_consumption` service to start fresh
- **Last Updated not changing**: This is normal if your tank level hasn't changed - it updates when BoilerJuice reports a different level
- **Sensors unavailable after upgrade**: Update dashboard to use new simplified sensor names (see Migration section)

### Debug Logging

To enable debug logging, add to your `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.boilerjuice: debug
```

### Getting Help

1. Check the [Home Assistant Community Forums](https://community.home-assistant.io/)
2. Open an issue in this repository with:
   - Home Assistant version
   - Integration version
   - Relevant logs (with debug enabled)
   - Description of the issue

## Contributing

Feel free to contribute to the development of this integration:

1. Fork the repository
2. Create a feature branch
3. Submit a Pull Request

## License

This integration is licensed under MIT License.
