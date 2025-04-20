# BoilerJuice Home Assistant Integration

This integration allows you to monitor your BoilerJuice oil tank level and consumption data in Home Assistant.

## Installation

1. Copy the `custom_components/boilerjuice` directory to your Home Assistant's `custom_components` directory.
2. Restart Home Assistant.
3. Go to Settings > Devices & Services > Add Integration
4. Search for "BoilerJuice" and click on it
5. Enter your BoilerJuice email and password
6. Optionally enter your tank ID if you have multiple tanks
7. Click Submit

## Features

The integration provides several sensors to monitor your oil tank:

### Sensors

- **Tank Level** - Current oil level as a percentage
- **Tank Volume** - Current volume of oil in liters
- **Tank Capacity** - Total tank capacity in liters
- **Tank Height** - Height of oil in the tank in centimeters
- **Days Until Empty** - Estimated days until the tank will be empty (based on consumption)
- **Daily Consumption** - Average daily consumption in liters
- **Total Consumption** - Total oil consumption in liters since last reset
- **Energy Consumption** - Oil consumption converted to kWh

The data is updated once per day to avoid excessive requests to the BoilerJuice website.

### Services

The integration provides the following services:

- **reset_consumption** - Resets the consumption counters to zero
  ```yaml
  service: boilerjuice.reset_consumption
  ```

## Configuration

The integration can be configured through the Home Assistant UI. You'll need to provide:

- **Email** (required): Your BoilerJuice account email
- **Password** (required): Your BoilerJuice account password
- **Tank ID** (optional): If you have multiple tanks, specify which one to monitor

## How It Works

The integration:

1. Logs into your BoilerJuice account using the provided credentials
2. Scrapes the tank data from the BoilerJuice website once per day
3. Processes the data to calculate:
   - Current tank levels and volumes
   - Consumption rates
   - Energy usage (using a conversion factor of 10.35 kWh per liter of oil)
   - Estimated days until empty based on usage patterns

The integration creates a device entry for your oil tank, grouping all sensors together for easy access in the Home Assistant UI.

## Troubleshooting

If you encounter any issues:

1. Check your credentials are correct
2. Verify your BoilerJuice account has access to the tank monitor feature
3. Check the Home Assistant logs for any error messages
4. If sensors show as "unknown":
   - This is normal for consumption data when first installed
   - Values will populate as the integration collects usage data
   - The "Days Until Empty" calculation requires consumption data to provide accurate estimates

Common issues:

- **Unknown values after installation**: Some sensors need time to collect data before showing values
- **Authentication errors**: Double-check your email and password
- **Multiple tanks**: Specify the tank ID in the configuration if you have more than one tank

## Support

For support:

1. Check the [Issues](https://github.com/yourusername/boilerjuice-scrape/issues) section for known problems
2. Open a new issue if you encounter a bug
3. Include your Home Assistant logs when reporting issues

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
