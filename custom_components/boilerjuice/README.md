# BoilerJuice Integration for Home Assistant

This integration allows you to monitor your BoilerJuice oil tank levels in Home Assistant.

## Features

- Monitor your oil tank level in real-time
- View your tank's current level as a percentage
- Track your oil usage over time
- Get notified when your tank level is low
- Track total oil consumption in both liters and kWh
- Monitor daily oil consumption
- Estimate days until tank is empty
- Reset consumption counter when needed

## Installation

1. Copy the `boilerjuice` folder to your Home Assistant's `custom_components` directory
2. Restart Home Assistant
3. Go to Settings > Devices & Services
4. Click "Add Integration"
5. Search for "BoilerJuice"
6. Enter your BoilerJuice account credentials:
   - Email address
   - Password
   - (Optional) Tank ID (if you have multiple tanks)

## Configuration

The integration is configured through the Home Assistant UI. No YAML configuration is required.

### Required Configuration

- **Email**: Your BoilerJuice account email address
- **Password**: Your BoilerJuice account password

### Optional Configuration

- **Tank ID**: If you have multiple tanks, specify the ID of the tank you want to monitor

## Sensors

The integration creates the following sensors:

- `sensor.boilerjuice_tank_level`: Shows the current tank level as a percentage
- `sensor.boilerjuice_tank_volume`: Shows the current tank volume in liters
- `sensor.boilerjuice_tank_capacity`: Shows the tank's total capacity in liters
- `sensor.boilerjuice_oil_consumption_liters`: Shows the total oil consumption in liters
- `sensor.boilerjuice_oil_consumption_kwh`: Shows the total oil consumption in kilowatt-hours
- `sensor.boilerjuice_daily_oil_consumption`: Shows the average daily oil consumption in liters
- `sensor.boilerjuice_days_until_empty`: Shows the estimated days until the tank is empty

### Energy Dashboard Integration

The kWh consumption sensor can be added to the Energy Dashboard to track your heating oil usage alongside other energy sources. The conversion from liters to kWh uses a standard conversion factor of 10.35 kWh per liter of heating oil.

### Days Until Empty

The days until empty estimate is calculated based on:

- Current tank volume
- Average daily consumption rate
- Updates once per day

This estimate assumes that your current consumption rate will remain constant. It will become more accurate as more consumption data is collected.

## Services

The integration provides the following service:

- `boilerjuice.reset_consumption`: Resets all consumption counters to zero

### Example Service Call

```yaml
service: boilerjuice.reset_consumption
```

## Troubleshooting

If you encounter any issues:

1. Check that your BoilerJuice credentials are correct
2. Verify that your tank is properly registered in your BoilerJuice account
3. Check the Home Assistant logs for any error messages
4. Make sure your tank's monitoring device is online and sending data to BoilerJuice

## Support

If you need help with this integration, please:

1. Check the [Home Assistant Community Forums](https://community.home-assistant.io/)
2. Open an issue on the GitHub repository
3. Contact the integration maintainer

## License

This integration is released under the MIT License.
