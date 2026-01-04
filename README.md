# BoilerJuice Integration for Home Assistant

This custom integration allows you to monitor your BoilerJuice oil tank details in Home Assistant.

## Features

- Monitor your oil tank levels and volumes
- Track oil consumption
- View current oil prices
- Calculate energy costs
- Estimate days until empty

## How It Works

The integration works by:

1. **Data Collection**:

   - Logs into your BoilerJuice account securely
   - Scrapes tank data from your account page
   - Fetches current oil prices from the kerosene prices page
   - Updates once per day to avoid excessive requests

2. **Tank Monitoring**:

   - Tracks both total and usable oil levels
   - Monitors total volume (710L) and usable volume (510L)
   - Records tank dimensions and capacity (1170L)
   - Calculates percentages for both total and usable oil

3. **Consumption Tracking**:

   - Calculates daily consumption based on volume changes
   - Maintains running totals of oil used
   - Converts oil consumption to energy (kWh)
   - Estimates days until empty based on usage patterns

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

- **Tank ID**: Your tank ID if you have multiple tanks
- **kWh per litre**: Energy content of your oil in kWh per litre (default: 10.35)

## Available Sensors

### Tank Levels

- **Total Oil Level** (%) - Total oil level as a percentage of tank capacity
- **Usable Oil Level** (%) - Usable oil level as a percentage of usable capacity

### Volumes

- **Oil Tank Volume** (L) - Current total volume of oil in the tank
- **Usable Oil Volume** (L) - Current usable volume of oil
- **Oil Tank Capacity** (L) - Total tank capacity

### Consumption

- **Daily Oil Consumption** (L) - Average daily oil consumption
- **Total Oil Consumption** (L) - Total oil consumed since last reset
- **Total Oil Consumption (kWh)** - Total energy consumed

### Cost and Energy

- **BoilerJuice Oil Price** (GBP/litre) - Current oil price per litre
- **Oil Energy Content** (kWh/L) - Energy content of your oil
- **Oil Cost per kWh** (GBP/kWh) - Current cost of energy from your oil

### Other

- **Days Until Empty** (days) - Estimated days until tank is empty
- **Oil Tank Height** (cm) - Physical height of your tank

## Services

### Reset Consumption

Resets the consumption counters to zero.

```yaml
service: boilerjuice.reset_consumption
```

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

## Troubleshooting

### Common Issues

- **Authentication errors**: Double-check your email and password
- **Missing data**: Ensure your BoilerJuice account is active and has a tank configured
- **Incorrect readings**: Verify your tank details on the BoilerJuice website

### Getting Help

1. Check the [Home Assistant Community Forums](https://community.home-assistant.io/)
2. Open an issue in this repository

## Contributing

Feel free to contribute to the development of this integration:

1. Fork the repository
2. Create a feature branch
3. Submit a Pull Request

## License

This integration is licensed under MIT License.
