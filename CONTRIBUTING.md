# Contributing

## Getting set up

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt -r requirements-test.txt
```

That gives you the linters and the current Home Assistant test lane. For the
minimum lane, install `requirements-test-min.txt` into a separate
environment: the two pin different Home Assistant versions and will fight
over the same one.

## Before you push

```bash
ruff format custom_components tests scripts
ruff check custom_components tests scripts
mypy
pytest --cov=custom_components.boilerjuice --cov-report=json
python scripts/check_module_coverage.py coverage.json 95
```

CI runs all of that, on both supported Home Assistant versions, plus
hassfest, HACS validation, `actionlint` and a dependency audit.

## Testing rules

- **The suite is offline.** Sockets are blocked and every BoilerJuice
  response comes from a fixture in `tests/fixtures/`. Do not add a test that
  needs real credentials or a network connection; there are none in CI.
- **Coverage is enforced per module**, not just in total, at 95%. A
  well-covered module must not be able to hide a barely-tested one.
- **Sanitise fixtures.** A page saved from your own account contains your
  account details. Strip them before committing.
- The scripts in `scripts/` are manual diagnostic tools that do talk to the
  live site. They live outside `tests/` and are not named `test_*` so pytest
  never collects them.

## Where things live

| Module | Responsibility |
| --- | --- |
| `client.py` | HTTP, sign-in, session lifecycle, response classification |
| `parser.py` | HTML to validated model. No network, no clock, no Home Assistant |
| `models.py` | `TankReading`: frozen, typed, with no zero defaults |
| `consumption.py` | Pure maths: transitions, daily allocation, rolling window, seasons |
| `tank.py` | One tank's running totals |
| `storage.py` | The durable document, its schema and its migrations |
| `coordinator.py` | Orchestration only |

Two rules the code depends on:

1. **A missing reading is `None`, never `0`.** "We could not read the volume"
   and "the tank is empty" must stay distinguishable through every layer.
   Substituting zero is what let a failed scrape record an entire tank of
   phantom consumption.
2. **Nothing mutates stored state until the whole reading has validated.** If
   a page did not parse, the previous state stands.

## Logging

Never log a password, an email address, a tank ID, an account name, a CSRF
token, a cookie or response HTML, at any level. Scraper logs end up in bug
reports.

## Pull requests

Branch, commit with a message that says what changed and why, and open a PR.
CI must pass. If you change behaviour, add or update a test that would have
caught the old behaviour.

## Releasing

1. Update `version` in `custom_components/boilerjuice/manifest.json`.
2. If the minimum Home Assistant version moves, update `hacs.json` and
   `requirements-test-min.txt` together, in the same commit.
3. Tag `vMAJOR.MINOR.PATCH` and push the tag.

The release workflow re-runs the whole of CI against that exact commit,
checks the tag matches the manifest, builds the archive, inspects its
contents, and only then publishes the release with a SHA-256 digest. A tag
whose commit fails CI publishes nothing.
