# BoilerJuice Integration for Home Assistant

[![CI](https://github.com/willbeeching/boilerjuice/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/boilerjuice/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/boilerjuice)](https://github.com/willbeeching/boilerjuice/releases)
[![License](https://img.shields.io/github/license/willbeeching/boilerjuice)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

Monitors BoilerJuice heating oil tanks in Home Assistant: level, volume,
consumption, energy content and cost.

## How it works, and what that costs you

BoilerJuice has no public API. This integration signs into your account with
your email and password and reads the tank page, once an hour.

That has consequences worth knowing before you install it:

- **It can break when BoilerJuice changes its website.** When that happens
  readings stop and a repair notification appears under Settings. Your
  consumption history is not lost: the integration refuses to record a
  reading it could not parse, rather than treating an unreadable page as an
  empty tank.
- **Your BoilerJuice password is stored by Home Assistant**, in the same
  plain-text config entry store every other login-based integration uses.
  See [SECURITY.md](SECURITY.md) for exactly what is stored, sent and
  logged.
- **Readings are only as fresh as BoilerJuice makes them.** The underlying
  tank level changes far less often than hourly, which is why consumption is
  derived from level changes rather than from poll timing.

## Requirements

- Home Assistant 2025.2.5 or newer. Both that version and the current
  release are tested in CI on every change.
- A BoilerJuice account with at least one tank set up.

## Installation

### HACS

BoilerJuice is not in the HACS default list, so add it as a custom
repository:

1. Open HACS.
2. Three dots menu, top right, then **Custom repositories**.
3. Add `https://github.com/willbeeching/boilerjuice` with category
   **Integration**.
4. Find BoilerJuice and click **Download**.
5. Restart Home Assistant.

### Manual

1. Download `boilerjuice.zip` from the
   [latest release](https://github.com/willbeeching/boilerjuice/releases) and
   extract it into `config/custom_components/`. Every release lists the
   archive's SHA-256 digest so you can check what you downloaded.
2. Restart Home Assistant.

### Adding your account

1. **Settings**, then **Devices & Services**, then **Add Integration**.
2. Search for BoilerJuice.
3. Enter your account email and password.

Setup fails if the credentials are rejected, if BoilerJuice cannot be
reached, or if the account has no tanks, so a successful setup means the
scrape worked.

One config entry is one BoilerJuice account. Every tank on the account
becomes its own device with its own sensors and its own consumption history.

### Removing it

**Settings**, **Devices & Services**, BoilerJuice, three dots, **Delete**.
That removes the account's devices and entities and deletes its stored
consumption history. Nothing is left behind, and nothing is changed on the
BoilerJuice website.

## Configuration

Set at the time you add the account:

| Option | Required | Default | Notes |
| --- | --- | --- | --- |
| Email | Yes | — | BoilerJuice account email |
| Password | Yes | — | BoilerJuice account password |
| kWh per litre | No | `10.35` | Energy content of your oil |

Changed afterwards through **Reconfigure** on the integration entry:

| Option | Notes |
| --- | --- |
| Password | Leave blank to keep the current one |
| kWh per litre | Applied to oil burnt from the next poll on, not retrospectively |
| Tanks to track | Which of the account's tanks get entities. Excluding a tank removes its device on the next poll and keeps its history in case you put it back |

If BoilerJuice stops accepting your password, Home Assistant raises a
**Reconfigure**-style repair asking for the new one. It does not silently
retry for ever.

Multiple accounts are supported. Each keeps its own login session, cookie
jar and stored history.

## Supported tanks

Whatever BoilerJuice itself supports. The integration reads what the tank
page reports and does not model tank shapes itself; the shape, model and
manufacturer are read from the page as attributes. A tank BoilerJuice tracks
is a tank this integration can read.

## Sensors

One set per tank.

### Tank

| Sensor | Unit | Notes |
| --- | --- | --- |
| Oil level | % | Not a battery device class: a tank is not a cell |
| Volume | L | Current volume of oil |
| Capacity | L | Total tank capacity (diagnostic) |
| Height | cm | Physical tank height (diagnostic) |

### Consumption

| Sensor | Unit | Notes |
| --- | --- | --- |
| Daily oil consumption | L/day | Rolling 7-day average. Unknown until a complete day has been measured. `sample_days` says how much evidence is behind it |
| Total oil consumption | L | Accumulates since the last reset |
| Total oil energy | kWh | Accumulated using the kWh per litre in force at the time. **This is the Energy dashboard sensor** |
| Seasonal oil consumption | L/day | Current season's average, with per-season and per-month figures as attributes |
| Days until empty | d | Current volume divided by the daily rate, falling back to an estimate of 2% of capacity per day when there is no history yet |

### Price and energy

| Sensor | Unit |
| --- | --- |
| Oil price | GBP/L |
| Oil energy content | kWh/L |
| Oil cost per kWh | GBP/kWh |

### Diagnostic

| Sensor | Notes |
| --- | --- |
| Last level change | When the tank level last changed. It is normal for this to sit still for days |
| Last successful update | When the account was last polled successfully |

## How consumption tracking works

BoilerJuice only reports a new tank level occasionally, so consumption is
inferred from the change between two readings and then apportioned across the
period it spanned.

```
Dec 1, 09:00   Tank at 850 L        reference saved
Dec 2 - Dec 5  Reading unchanged    reference stays at 850 L
Dec 6, 09:00   Tank at 800 L        50 L used over the last 5 days
```

That 50 L is split across the days by how much of the interval fell in each,
so the parts add back to exactly 50 L:

```
Dec 1   6.25 L   (09:00 to midnight)
Dec 2  10.00 L
Dec 3  10.00 L
Dec 4  10.00 L
Dec 5  10.00 L
Dec 6   3.75 L   (midnight to 09:00)
```

Those daily totals feed the rolling 7-day average behind Daily oil
consumption, and the per-season buckets behind Seasonal oil consumption.

Other behaviour worth knowing:

- The rolling average uses complete days only. Today's bucket is still
  filling, so including it would drag the rate down: a day three hours old
  holds three hours of oil but would carry a full day of weight.
- Refills are detected rather than counted as negative consumption. When the
  level rises, the reference resets without discarding history.
- The reference only moves when the level moves, so a flat reading does not
  dilute the rate.
- A drop spanning midnight is split between the two days by the time in
  each, not credited whole to the day it was noticed.
- Weighting is by elapsed time, so a 23- or 25-hour daylight-saving day
  neither gains nor loses oil.
- Dated history is kept for 400 days, collapsed to one row per day. That is
  just over a year so each season has data to average against.

### What the estimates cannot tell you

- **Days until empty is a straight-line projection.** It divides what is in
  the tank by your recent average. It knows nothing about the weather, and a
  cold snap will beat it.
- **Before any history exists** it falls back to assuming 2% of tank capacity
  a day. That is a placeholder, not a measurement.
- **Consumption is inferred, not metered.** It is the difference between two
  readings BoilerJuice published, so its resolution is theirs.
- **Seasonal averages need time.** Seasons are Winter (Dec to Feb), Spring
  (Mar to May), Summer (Jun to Aug) and Autumn (Sep to Nov). A season reports
  unknown until the integration has observed consumption during it. On a
  fresh install the current season populates quickly and the other three fill
  in as the year goes round. This is a data availability limit, not a fault.

## Actions

### `boilerjuice.reset_consumption`

Zeroes the consumption counters and history for the target, clears any manual
daily override, and takes the current level as the new baseline.

```yaml
action: boilerjuice.reset_consumption
target:
  device_id: <your BoilerJuice tank>
```

A device or entity target resets that tank. Targeting the config entry resets
every tank on the account. With more than one account configured a target is
required: this action rewrites stored history, so it will not guess.

### `boilerjuice.set_consumption`

Seeds the counters with known values, for example after a reset or after
moving from another integration.

```yaml
action: boilerjuice.set_consumption
target:
  device_id: <your BoilerJuice tank>
data:
  liters: 500 # Total litres consumed
  daily: 15 # Optional: daily rate in L/day
```

`daily` is a **persistent override**. It replaces the measured rate and
survives polls and restarts until you run `reset_consumption`. Leave it out
to keep using the measured rate.

## Automation examples

Warn when the tank is getting low:

```yaml
automation:
  - alias: Oil tank running low
    triggers:
      - trigger: numeric_state
        entity_id: sensor.garden_tank_oil_level
        below: 25
    actions:
      - action: notify.persistent_notification
        data:
          message: >-
            Oil tank at {{ states('sensor.garden_tank_oil_level') }}%,
            about {{ states('sensor.garden_tank_days_until_empty') }} days left.
```

Order before winter bites, using the days-until-empty estimate:

```yaml
automation:
  - alias: Time to order oil
    triggers:
      - trigger: numeric_state
        entity_id: sensor.garden_tank_days_until_empty
        below: 21
    conditions:
      - condition: numeric_state
        entity_id: sensor.garden_tank_oil_price
        below: 0.65
    actions:
      - action: notify.mobile_app
        data:
          message: >-
            Three weeks of oil left and the price is
            {{ states('sensor.garden_tank_oil_price') }} a litre.
```

Add the tank to the Energy dashboard by configuring **Total oil energy** as a
gas or energy source under Settings, Dashboards, Energy.

## Upgrading

### To 2.0.0

This is a breaking release. Read this section before upgrading.

**One config entry is now one account, and every tank on it is a device.**
Previously the integration picked the first tank and ignored the rest. If
your account has more than one tank you will get devices and entities for all
of them on the first poll after upgrading. Use **Reconfigure** to narrow that
down.

**Existing entities keep their entity IDs.** Entities and devices are keyed
by tank ID, which has not changed, so dashboards and automations keep
working. Their unique IDs move from Python class names to stable keys, which
Home Assistant handles automatically on first start.

**Two entities change:**

| Entity | Change |
| --- | --- |
| `sensor.<tank>_oil_consumption_kwh` | **Removed.** It accumulated its value as a side effect of being read, so what it reported depended on how often something looked at it. Use **Total oil energy** on the Energy dashboard instead, now that its conversion honours your configured kWh per litre |
| `sensor.<tank>_last_updated` | Renamed to **Last level change**, which is what it has always measured. A new **Last successful update** sensor reports when the account was last polled |

If the Energy dashboard was configured against the removed sensor, point it
at Total oil energy. Its historical statistics do not carry across.

**Other changes you will notice:**

- Oil level is no longer a battery device class, so it leaves battery
  dashboards and low-battery alerts.
- Sensors report unknown rather than 0 when there is nothing to report: a
  daily rate before a complete day has been measured, a season with no data.
- Changing kWh per litre now affects every kWh figure. Previously the total
  was hard-coded to 10.35 while the cost sensors used your configured value.
  It applies to oil burnt from that point on, not retrospectively: Total oil
  energy is a `total_increasing` sensor, so restating its history would show
  up in long-term statistics as a jump, or as a meter reset if the number
  went down.
- Consumption history moves to a per-account storage document, automatically,
  on first start. If it cannot be read it is discarded and you get a repair
  notification saying so, rather than the history silently resetting.
- YAML configuration is deprecated. It still imports once, and logs a warning
  asking you to remove the block.

### To 1.3.2

A failed scrape could be accepted as a valid reading: a page the parser did
not understand became "0 litres, 0%", and the drop from the last good reading
to that zero was recorded as real consumption. If your history contains an
implausible spike, run `boilerjuice.reset_consumption` and re-seed with
`boilerjuice.set_consumption`.

### To 1.3.1

Days until empty had two implementations, and the one the sensor used fell
back to a hard-coded 510 L rather than your tank's real capacity. It only
showed on a fresh install, before any consumption history exists.

### To 1.3.0

- Seasonal oil consumption now works. It previously reported unknown on most
  polls and could only ever populate one season.
- Consumption figures shift slightly. Multi-day consumption used to be
  over-attributed; daily totals are now conserved exactly.
- Setup now rejects bad credentials, rather than creating an entry that
  silently never updates.

### From 1.0.x

Duplicate sensors were merged:

| Old | New |
| --- | --- |
| `sensor.<tank>_total_oil_level`, `sensor.<tank>_usable_oil_level` | `sensor.<tank>_oil_level` |
| `sensor.<tank>_usable_oil_volume` | `sensor.<tank>_volume` |

## Troubleshooting

| Symptom | Cause or fix |
| --- | --- |
| A "sign in again" repair appeared | Your BoilerJuice password changed, or the account was locked. Click through the repair and enter the current password |
| A "BoilerJuice has changed its website" repair appeared | The site's layout moved. Check for an integration update; if there is not one, open a "Readings have stopped" issue |
| A "consumption history was reset" repair appeared | The stored history could not be read and was discarded. Current readings are unaffected. Re-seed the totals with `boilerjuice.set_consumption` if you know them |
| One tank unavailable, the others fine | That tank could not be read. Its history is safe; it becomes available again on the first successful poll. The log carries one warning per outage, not one per hour |
| Entities unavailable | The last poll failed. Check the logs; the integration retries hourly |
| Consumption stuck at 0 | Expected until BoilerJuice publishes a lower level than the one recorded as the reference |
| Daily consumption unknown | Expected until a complete day has been measured. `sample_days` on the sensor says how many days it has |
| Last level change not moving | Expected. It tracks level changes, not polls. Use Last successful update to check polling |
| Seasonal averages unknown | Expected for seasons the integration has not lived through yet |

### Recovering from a bad history

If consumption looks wrong, for example after upgrading from a version that
could record a phantom reading:

1. Run `boilerjuice.reset_consumption` against the tank. That clears its
   counters, history and references.
2. Optionally run `boilerjuice.set_consumption` with the total you believe is
   correct, so long-term statistics continue from the right place.
3. Leave it a day. The daily rate reappears once a complete day has been
   measured, and seasons refill as the year goes round.

The tank's current level, volume and capacity are read fresh every poll and
are never affected by any of this.

### Debug logging

```yaml
logger:
  default: info
  logs:
    custom_components.boilerjuice: debug
```

### Getting help

Open an issue using one of the templates. Attach the diagnostics download
(**Settings**, **Devices & Services**, BoilerJuice, three dots, **Download
diagnostics**): it is written to contain no email address, password, tank ID,
tank name, cookie or page HTML, so it is safe to post publicly.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the module layout and the
two invariants the code depends on.

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt -r requirements-test.txt
.venv/bin/ruff format custom_components tests scripts
.venv/bin/ruff check custom_components tests scripts
.venv/bin/mypy
.venv/bin/python -m pytest
```

The two test lanes need different Python versions (3.14 for current Home
Assistant, 3.13 for the 2025.2.5 floor), so they need separate environments.
See [CONTRIBUTING.md](CONTRIBUTING.md).

The test suite is entirely offline: it uses sanitised HTML fixtures, blocks
sockets, and needs no BoilerJuice credentials. Coverage is enforced at 95%
per module, on both lanes.

`quality_scale.yaml` tracks this integration against the
[Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/)
rule by rule. The manifest deliberately claims no quality scale while any
Bronze rule is still outstanding.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). CI must pass.

## Security

See [SECURITY.md](SECURITY.md) for how credentials are handled and how to
report a vulnerability.

## License

MIT, see [LICENSE](LICENSE).
