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
3. Tag `v` plus that version and push the tag.

A prerelease is a version with a suffix: `2.0.0-beta.1`, `2.0.0-rc.2`. The
manifest carries it like any other version, and the workflow reads it to
decide how to publish, so there is no separate flag to remember. GitHub marks
the release as a pre-release and leaves "latest" where it was, and HACS
offers it only to people who have turned beta versions on.

The release workflow re-runs the whole of CI against that exact commit,
checks the tag matches the manifest, builds the archive, inspects its
contents, and only then publishes the release with a SHA-256 digest. A tag
whose commit fails CI publishes nothing.

A version is published once. The workflow refuses to run against a tag that
already has a release, because the publish step would otherwise replace its
archive and the digest people were told to check. To correct a bad release,
delete it deliberately and tag a new version rather than moving the old tag.

Two repository settings back that up, and neither lives in this repo:

- **Immutable releases**, under Settings, so a published release and its
  assets cannot be edited at all.
- **A tag ruleset** for `v*.*.*` that forbids updating and deleting tags, so
  a tag cannot be moved onto a different commit.
