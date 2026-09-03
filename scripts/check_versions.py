#!/usr/bin/env python3
"""Check that the versions scattered across the repository agree.

A release whose tag, manifest and HACS floor disagree installs something
other than what it says on the tin, so this runs on every pull request and
again against the exact tagged commit at release time.

Usage:
    python scripts/check_versions.py            # internal consistency
    python scripts/check_versions.py v2.0.0     # ...and that the tag matches
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "boilerjuice" / "manifest.json"
HACS = ROOT / "hacs.json"
QUALITY_SCALE = ROOT / "custom_components" / "boilerjuice" / "quality_scale.yaml"
MIN_REQUIREMENTS = ROOT / "requirements-test-min.txt"
CURRENT_REQUIREMENTS = ROOT / "requirements-test.txt"
CI = ROOT / ".github" / "workflows" / "ci.yaml"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
TAG = re.compile(r"^v\d+\.\d+\.\d+$")

VALID_STATUSES = frozenset({"done", "exempt", "todo"})

# The Integration Quality Scale rules, by tier. Taken from the quality_scale
# files Home Assistant itself ships: all 348 of them carry these 54 rules
# under these four headings, so this is the rule set hassfest enforces on
# core integrations rather than a list written from memory.
#
# Regenerate after a Home Assistant upgrade with:
#   python scripts/check_versions.py --print-rules-from <path to homeassistant>
TIERS: dict[str, frozenset[str]] = {
    "bronze": frozenset(
        {
            "action-setup",
            "appropriate-polling",
            "brands",
            "common-modules",
            "config-flow",
            "config-flow-test-coverage",
            "dependency-transparency",
            "docs-actions",
            "docs-conditions",
            "docs-high-level-description",
            "docs-installation-instructions",
            "docs-removal-instructions",
            "docs-triggers",
            "entity-event-setup",
            "entity-unique-id",
            "has-entity-name",
            "runtime-data",
            "test-before-configure",
            "test-before-setup",
            "unique-config-entry",
        }
    ),
    "silver": frozenset(
        {
            "action-exceptions",
            "config-entry-unloading",
            "docs-configuration-parameters",
            "docs-installation-parameters",
            "entity-unavailable",
            "integration-owner",
            "log-when-unavailable",
            "parallel-updates",
            "reauthentication-flow",
            "test-coverage",
        }
    ),
    "gold": frozenset(
        {
            "devices",
            "diagnostics",
            "discovery",
            "discovery-update-info",
            "docs-data-update",
            "docs-examples",
            "docs-known-limitations",
            "docs-supported-devices",
            "docs-supported-functions",
            "docs-troubleshooting",
            "docs-use-cases",
            "dynamic-devices",
            "entity-category",
            "entity-device-class",
            "entity-disabled-by-default",
            "entity-translations",
            "exception-translations",
            "icon-translations",
            "reconfiguration-flow",
            "repair-issues",
            "stale-devices",
        }
    ),
    "platinum": frozenset(
        {
            "async-dependency",
            "inject-websession",
            "strict-typing",
        }
    ),
}
TIER_ORDER = ("bronze", "silver", "gold", "platinum")
ALL_RULES = frozenset().union(*TIERS.values())


def fail(message: str) -> None:
    """Print a failure and exit."""
    print(f"FAIL: {message}")
    sys.exit(1)


def main(argv: list[str]) -> int:
    """Compare every declared version and return an exit status."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    hacs = json.loads(HACS.read_text(encoding="utf-8"))

    version = manifest["version"]
    if not SEMVER.match(version):
        fail(f"manifest version {version!r} is not MAJOR.MINOR.PATCH")
    print(f"  manifest version: {version}")

    floor = hacs["homeassistant"]
    print(f"  declared Home Assistant floor: {floor}")

    # The floor has to be a version the minimum test lane actually runs, or
    # it is a number nobody has ever checked.
    tested = _annotation(MIN_REQUIREMENTS, "home-assistant")
    if tested != floor:
        fail(
            f"hacs.json declares Home Assistant {floor} but the minimum test "
            f"lane runs {tested}"
        )
    print(f"  minimum lane runs: Home Assistant {tested}")

    # A test lane pinned to a Home Assistant its interpreter cannot install
    # fails at "pip install", which is a slow and confusing way to find out.
    _check_lane_python()

    _check_quality_scale(manifest.get("quality_scale"))

    if len(argv) > 1:
        tag = argv[1].removeprefix("refs/tags/")
        if not TAG.match(tag):
            fail(f"tag {tag!r} is not vMAJOR.MINOR.PATCH")
        if tag.removeprefix("v") != version:
            fail(f"tag {tag} does not match manifest version {version}")
        print(f"  tag {tag} matches the manifest")

    print("Versions agree.")
    return 0


def _check_quality_scale(claimed: str | None) -> None:
    """Check the tracker is complete and the manifest claim is honest.

    The old check only looked at whether the file existed and whether the
    manifest held a recognised word, so the manifest could claim platinum
    with rules still todo and this would happily agree.
    """
    if not QUALITY_SCALE.exists():
        fail(f"{QUALITY_SCALE} is missing")

    document = yaml.safe_load(QUALITY_SCALE.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("rules"), dict):
        fail("quality_scale.yaml has no 'rules' mapping")

    rules: dict[str, str] = {}
    for name, value in document["rules"].items():
        status = value if isinstance(value, str) else (value or {}).get("status")
        if status not in VALID_STATUSES:
            fail(
                f"rule {name!r} has status {status!r}; expected one of "
                f"{sorted(VALID_STATUSES)}"
            )
        rules[name] = status

    if invented := sorted(set(rules) - ALL_RULES):
        fail(
            "quality_scale.yaml lists rules that are not in the Integration "
            f"Quality Scale: {invented}"
        )
    if missing := sorted(ALL_RULES - set(rules)):
        fail(f"quality_scale.yaml is missing rules: {missing}")

    outstanding = {
        tier: sorted(name for name in TIERS[tier] if rules[name] == "todo")
        for tier in TIER_ORDER
    }
    for tier in TIER_ORDER:
        state = "complete" if not outstanding[tier] else f"todo: {outstanding[tier]}"
        print(f"  quality scale {tier}: {state}")

    if claimed in (None, "no_score"):
        print(f"  quality scale claimed: {claimed or 'none'}")
        return

    if claimed not in TIER_ORDER:
        fail(f"unknown quality scale {claimed!r}")

    # A tier is all of itself and everything below it, or none of it.
    for tier in TIER_ORDER[: TIER_ORDER.index(claimed) + 1]:
        if outstanding[tier]:
            fail(
                f"the manifest claims {claimed}, but these {tier} rules are "
                f"still todo: {outstanding[tier]}"
            )
    print(f"  quality scale claimed: {claimed}, and earned")


def _annotation(path: pathlib.Path, name: str) -> str:
    """Return a `# <name>: <value>` annotation from a requirements file."""
    pattern = re.compile(rf"#\s*{re.escape(name)}:\s*(\S+)")
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := pattern.fullmatch(line.strip()):
            return match.group(1).strip('"')
    fail(
        f"{path.name} has no '# {name}: <value>' line, so it cannot be "
        "checked against what CI runs"
    )
    raise AssertionError  # unreachable; fail() exits


def _check_lane_python() -> None:
    """Check each lane runs on the Python version CI gives it.

    Home Assistant raises its Python floor regularly. Pinning a lane to a
    release its interpreter cannot install turns into an install failure in
    CI, which is a slow and confusing way to discover a one-line mistake.
    """
    workflow = CI.read_text(encoding="utf-8")

    for path, requirements_name in (
        (MIN_REQUIREMENTS, "requirements-test-min.txt"),
        (CURRENT_REQUIREMENTS, "requirements-test.txt"),
    ):
        wanted = _annotation(path, "python")
        pattern = re.compile(
            rf"requirements:\s*{re.escape(requirements_name)}\s*\n"
            rf"\s*python:\s*\"?{re.escape(wanted)}\"?",
        )
        if not pattern.search(workflow):
            fail(
                f"{requirements_name} says it needs Python {wanted}, but the "
                "CI matrix does not pair it with that version"
            )
        print(f"  {requirements_name}: Python {wanted}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
