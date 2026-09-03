# Contributing

## Getting set up

The two test lanes need **different Python versions**, so they need
different environments. Home Assistant raises its Python floor regularly,
and the supported floor here is an older release than the current one.

| Lane | Python | Home Assistant | Requirements |
| --- | --- | --- | --- |
| Current | 3.14 | 2026.9.0 | `requirements-test.txt` |
| Minimum | 3.13 | 2025.2.5 | `requirements-test-min.txt` |

Linting and type checking run on the current lane's interpreter, because
mypy has to parse the annotations that version of Home Assistant ships.

```bash
# Current lane, plus the linters. This is the one to work in.
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt -r requirements-test.txt

# Minimum lane, for checking the floor still works.
uv venv --python 3.13 .venv-min
uv pip install --python .venv-min/bin/python -r requirements-test-min.txt
```

Each requirements file carries `# home-assistant:` and `# python:`
annotations. `scripts/check_versions.py` checks them against the CI matrix,
so a lane cannot end up pinned to a Home Assistant its interpreter cannot
install. Run it after touching either file.

## Before you push

```bash
.venv/bin/ruff format custom_components tests scripts
.venv/bin/ruff check custom_components tests scripts
.venv/bin/mypy
.venv/bin/python scripts/check_versions.py

# Both lanes, because a change can pass on one and fail on the other.
.venv/bin/python -m pytest --cov=custom_components.boilerjuice --cov-report=json
.venv/bin/python scripts/check_module_coverage.py coverage.json 95
.venv-min/bin/python -m pytest
```

CI runs all of that, on both supported Home Assistant versions, plus
hassfest, HACS validation, `actionlint` and a dependency audit.

Differences between the lanes are real bugs, not noise: whether a config
entry still counts as loaded during its own unload, and which
device-registry lookups exist, both vary across the supported range. See
`helpers.py` for the compatibility seam and `tests/test_ha_compat.py` for
how both sides of it are tested without needing both lanes.

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
