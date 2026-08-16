# BoilerJuice Integration for Home Assistant

[![CI](https://github.com/willbeeching/boilerjuice/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/boilerjuice/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/boilerjuice)](https://github.com/willbeeching/boilerjuice/releases)
[![License](https://img.shields.io/github/license/willbeeching/boilerjuice)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/hacs/integration)

Monitors a BoilerJuice heating oil tank in Home Assistant: level, volume, consumption, energy content and cost.

BoilerJuice has no public API, so this integration signs into your account and reads the tank page. It polls once an hour. The underlying readings update far less often than that, which is why consumption is derived from level changes rather than from poll timing.

## Installation

### HACS

1. Open HACS, then Integrations.
2. Three dots menu, top right, then Custom repositories.
3. Add `https://github.com/willbeeching/boilerjuice` with category Integration.
4. Find BoilerJuice and click Download.
5. Restart Home Assistant.

### Manual

1. Download `boilerjuice.zip` from the [latest release](https://github.com/willbeeching/boilerjuice/releases) and extract it into `config/custom_components/`. Alternatively copy the `custom_components/boilerjuice` folder from this repository.
2. Restart Home Assistant.

### Adding the integration

1. Settings, then Devices & Services, then Add Integration.
2. Search for BoilerJuice.
3. Enter your account email and password.

Setup fails if the credentials are rejected or BoilerJuice cannot be reached, so a successful setup means the scrape worked.

## Configuration

| Option | Required | Default | Notes |
| --- | --- | --- | --- |
| Email | Yes | — | BoilerJuice account email |
| Password | Yes | — | BoilerJuice account password |
| Tank ID | No | auto-detected | Only needed if the account has multiple tanks |
| kWh per litre | No | `10.35` | Energy content of your oil |

Multiple accounts are supported. Each config entry keeps its own login session and its own stored consumption history.

## Sensors

### Tank

| Sensor | Unit | Notes |
| --- | --- | --- |
| Oil Level | % | Device class `battery`, so it renders with a battery icon |
| Tank Volume | L | Current volume of oil |
| Tank Capacity | L | Total tank capacity |
| Tank Height | cm | Physical tank height |

### Consumption

| Sensor | Unit | Notes |
| --- | --- | --- |
| Daily Oil Consumption | L/day | Rolling 7-day average |
| Total Oil Consumption | L | Accumulates since the last reset |
| Total Oil Consumption (kWh) | kWh | The above, converted |
| Oil Consumption (kWh) | kWh | Incremental. Use this one on the Energy dashboard |
| Seasonal Oil Consumption | L/day | Current season's average, with per-season and per-month figures as attributes |
| Days Until Empty | days | Current volume divided by the daily rate, falling back to an estimate of 2% of capacity per day when there is no history yet |

### Price and energy

| Sensor | Unit |
| --- | --- |
| BoilerJuice Oil Price | GBP/litre |
| Oil Energy Content | kWh/L |
| Oil Cost Per kWh | GBP/kWh |

### Diagnostic

| Sensor | Notes |
| --- | --- |
| Last Updated | When the tank level last changed, not when the integration last polled. It is normal for this to sit still for days |

## How consumption tracking works

BoilerJuice only reports a new tank level occasionally, so consumption is inferred from the change between two readings and then apportioned across the period it spanned.

```
Dec 1, 09:00   Tank at 850 L        reference saved
Dec 2 - Dec 5  Reading unchanged    reference stays at 850 L
Dec 6, 09:00   Tank at 800 L        50 L used over the last 5 days
```

That 50 L is split across the days by how much of the interval fell in each, so the parts add back to exactly 50 L:

```
Dec 1   6.25 L   (09:00 to midnight)
Dec 2  10.00 L
Dec 3  10.00 L
Dec 4  10.00 L
Dec 5  10.00 L
Dec 6   3.75 L   (midnight to 09:00)
```

Those daily totals feed the rolling 7-day average behind Daily Oil Consumption, and the per-season buckets behind Seasonal Oil Consumption.

Other behaviour worth knowing:

- Refills are detected rather than counted as negative consumption. When the level rises, the reference resets without discarding history.
- The reference only moves when the level moves, so a flat reading does not dilute the rate.
- Dated history is kept for 400 days, collapsed to one row per day. That is just over a year so each season has data to average against.

### Seasonal averages need time

Seasons are Winter (Dec to Feb), Spring (Mar to May), Summer (Jun to Aug) and Autumn (Sep to Nov).

A season only reports once the integration has observed consumption during it. On a fresh install the current season populates quickly, but the other three read `0` until the integration has been running long enough to reach them. This is a data availability limit, not a fault.

## Services

### `boilerjuice.reset_consumption`

Zeroes the consumption counters and takes the current level as the new baseline. Use it after upgrading from an old version to clear stuck reference values.

```yaml
service: boilerjuice.reset_consumption
```

### `boilerjuice.set_consumption`

Seeds the counters with known values.

```yaml
service: boilerjuice.set_consumption
data:
  liters: 500 # Total litres consumed
  daily: 15 # Optional: daily rate in L/day
```

## Upgrading

### To 1.3.0

- Seasonal Oil Consumption now works. It previously reported `unknown` on most polls and could only ever populate one season. Existing installs start reporting straight away, but the remaining seasons fill in as history accrues: the fix stops data being discarded, it cannot recover what was already thrown away.
- Consumption figures shift slightly. Multi-day consumption used to be over-attributed; the daily totals are now conserved exactly.
- Setup now rejects bad credentials. Previously a config flow with a wrong password could create an entry that silently never updated. Remove and re-add any entry in that state.

### From 1.0.x

Duplicate sensors were merged:

| Old | New |
| --- | --- |
| `sensor.<tank>_total_oil_level`, `sensor.<tank>_usable_oil_level` | `sensor.<tank>_oil_level` |
| `sensor.<tank>_usable_oil_volume` | `sensor.<tank>_tank_volume` |

After upgrading, update any dashboards and automations referencing the old entities, run `boilerjuice.reset_consumption` to clear stuck reference values, and remove the now-unavailable entities from the entity registry.

## Troubleshooting

| Symptom | Cause or fix |
| --- | --- |
| Authentication errors | Re-check email and password on the BoilerJuice website |
| Missing data | Confirm the account is active and has a tank configured |
| Consumption stuck at 0 | Run `boilerjuice.reset_consumption` |
| Last Updated not changing | Expected. It tracks level changes, not polls |
| Seasonal averages showing 0 | Expected for seasons the integration has not lived through yet |
| Sensors unavailable after upgrade | Update dashboards to the new sensor names, see Upgrading |

### Debug logging

```yaml
logger:
  default: info
  logs:
    custom_components.boilerjuice: debug
```

### Getting help

Open an issue including your Home Assistant version, the integration version, a description of the problem, and relevant logs with debug enabled.

## Development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

CI runs `black`, `isort` and `flake8` over `custom_components/boilerjuice/`, plus HACS and hassfest validation. Run the formatters before pushing:

```bash
black custom_components/boilerjuice/ && isort custom_components/boilerjuice/
```

There is no automated test suite. `test_boilerjuice.py` and `test_coordinator_parsing.py` are manual scratch scripts for poking at the scraper: the first signs into the live site using credentials from a `.env` file, the second needs a saved `tank_page.html` fixture that is not in the repository.

```bash
# .env
BOILERJUICE_EMAIL=your_email@example.com
BOILERJUICE_PASSWORD=your_password
```

## Contributing

Fork, branch, and open a pull request. CI must pass.

## License

MIT, see [LICENSE](LICENSE).
