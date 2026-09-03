"""The version and quality-scale gate.

A gate nobody has watched fail is not a gate: this checks it rejects each
thing it claims to reject, and that its copy of the Integration Quality
Scale rule set still matches what Home Assistant ships.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "boilerjuice" / "manifest.json"
TRACKER = ROOT / "custom_components" / "boilerjuice" / "quality_scale.yaml"


def load_checker():
    """Import scripts/check_versions.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "check_versions", ROOT / "scripts" / "check_versions.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def checker():
    """Return the checker, restoring the files it reads afterwards."""
    module = load_checker()
    manifest, tracker = MANIFEST.read_text(), TRACKER.read_text()
    try:
        yield module
    finally:
        MANIFEST.write_text(manifest)
        TRACKER.write_text(tracker)


def rules() -> dict:
    """Return the tracker's rules."""
    return yaml.safe_load(TRACKER.read_text())["rules"]


def write_rules(updated: dict) -> None:
    """Write the tracker back with `updated` rules."""
    TRACKER.write_text(yaml.safe_dump({"rules": updated}))


def claim(tier: str | None) -> None:
    """Set the manifest's quality scale claim."""
    manifest = json.loads(MANIFEST.read_text())
    if tier is None:
        manifest.pop("quality_scale", None)
    else:
        manifest["quality_scale"] = tier
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")


# --- the rule set itself --------------------------------------------------


def test_the_rule_set_covers_what_home_assistant_ships() -> None:
    """Home Assistant adds rules; a stale copy here would wave them through.

    Read from the quality_scale.yaml files Home Assistant ships with its own
    integrations, which is the set hassfest enforces on core.
    """
    import collections

    import homeassistant.components as components

    module = load_checker()
    root = pathlib.Path(components.__file__).parent
    files = sorted(root.glob("*/quality_scale.yaml"))
    if not files:
        pytest.skip("this Home Assistant does not ship quality_scale.yaml files")

    shipped: set[str] = set()
    placements: dict[str, collections.Counter] = {
        tier: collections.Counter() for tier in module.TIER_ORDER
    }

    for path in files:
        tier = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if heading := re.match(r"\s*#\s*(Bronze|Silver|Gold|Platinum)\s*$", line):
                tier = heading.group(1).lower()
                continue
            if (rule := re.match(r"  ([a-z0-9-]+):", line)) and tier:
                shipped.add(rule.group(1))
                placements[tier][rule.group(1)] += 1

    # A superset, not equality: the minimum lane runs an older Home Assistant
    # that knows fewer rules than the current one.
    assert shipped <= module.ALL_RULES, sorted(shipped - module.ALL_RULES)

    # A few integrations format their tier headings differently, which makes
    # the rules after one get attributed to the tier above. Only trust a
    # placement the overwhelming majority of files agree on.
    threshold = len(files) * 0.9
    for tier, counted in placements.items():
        agreed = {name for name, count in counted.items() if count > threshold}
        assert agreed <= module.TIERS[tier], sorted(agreed - module.TIERS[tier])


def test_the_tracker_is_complete_and_honest(checker) -> None:
    """The repository's own tracker must pass its own gate."""
    assert checker.main(["check_versions.py"]) == 0


# --- what the gate must reject --------------------------------------------


@pytest.mark.parametrize("tier", ["bronze", "silver", "gold", "platinum"])
def test_a_claim_is_refused_while_a_bronze_rule_is_todo(checker, tier: str) -> None:
    """The gate used to accept "platinum" with rules still outstanding."""
    outstanding = [
        name
        for name, value in rules().items()
        if (value if isinstance(value, str) else value["status"]) == "todo"
    ]
    assert outstanding, "this test needs at least one todo rule to be meaningful"

    claim(tier)

    with pytest.raises(SystemExit) as exit_info:
        checker.main(["check_versions.py"])
    assert exit_info.value.code == 1


def test_a_claim_is_accepted_once_its_tier_is_finished(checker) -> None:
    updated = rules()
    for name in updated:
        if (
            updated[name] if isinstance(updated[name], str) else updated[name]["status"]
        ) == "todo":
            updated[name] = {"status": "done"}
    write_rules(updated)
    claim("platinum")

    assert checker.main(["check_versions.py"]) == 0


def test_an_invented_rule_is_refused(checker) -> None:
    """Three invented rule names once sat in this tracker unnoticed."""
    updated = rules()
    updated["entity-triggers"] = {"status": "exempt"}
    write_rules(updated)

    with pytest.raises(SystemExit):
        checker.main(["check_versions.py"])


def test_a_missing_rule_is_refused(checker) -> None:
    updated = rules()
    del updated["strict-typing"]
    write_rules(updated)

    with pytest.raises(SystemExit):
        checker.main(["check_versions.py"])


def test_an_unknown_status_is_refused(checker) -> None:
    updated = rules()
    updated["brands"] = {"status": "probably"}
    write_rules(updated)

    with pytest.raises(SystemExit):
        checker.main(["check_versions.py"])


def test_an_unknown_tier_is_refused(checker) -> None:
    claim("diamond")

    with pytest.raises(SystemExit):
        checker.main(["check_versions.py"])


@pytest.mark.parametrize("tag", ["v2.0", "2.0.0", "v2.0.0-rc1", "v2.0.0; rm -rf /"])
def test_a_tag_that_is_not_strict_semver_is_refused(checker, tag: str) -> None:
    with pytest.raises(SystemExit):
        checker.main(["check_versions.py", tag])


def test_a_tag_that_disagrees_with_the_manifest_is_refused(checker) -> None:
    with pytest.raises(SystemExit):
        checker.main(["check_versions.py", "v9.9.9"])


def test_the_matching_tag_is_accepted(checker) -> None:
    version = json.loads(MANIFEST.read_text())["version"]

    assert checker.main(["check_versions.py", f"v{version}"]) == 0


# --- the requirement files ------------------------------------------------


def dev_requirements() -> dict[str, str]:
    """Return the exact pins in requirements-dev.txt, by normalised name."""
    pins: dict[str, str] = {}
    for line in (
        (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    ):
        line = line.split("#", 1)[0].strip()
        if match := re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;]+)", line):
            pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def test_no_dev_pin_collides_with_home_assistant() -> None:
    """CI installs this file alongside the current test lane.

    Home Assistant pins many packages exactly, so an exact pin here for one
    of them is an unsatisfiable resolution rather than a version preference:
    `pip install -r requirements-dev.txt -r requirements-test.txt` fails
    outright. PyYAML==6.0.2 did exactly that against Home Assistant's
    PyYAML==6.0.3.
    """
    import importlib.metadata as metadata

    from homeassistant.const import __version__ as installed

    # Only the current lane co-installs requirements-dev.txt, so only that
    # lane's pins are the ones that have to resolve together.
    module = load_checker()
    current = module._annotation(ROOT / "requirements-test.txt", "home-assistant")
    if installed != current:
        pytest.skip(
            f"requirements-dev.txt is installed with {current}, not {installed}"
        )

    ha_pins = {}
    for requirement in metadata.requires("homeassistant") or []:
        if match := re.match(r"([A-Za-z0-9._-]+)==([^\s;,]+)", requirement.strip()):
            ha_pins[match.group(1).lower().replace("_", "-")] = match.group(2)

    assert ha_pins, "expected Home Assistant to pin something"

    collisions = {
        name: (pin, ha_pins[name])
        for name, pin in dev_requirements().items()
        if name in ha_pins and ha_pins[name] != pin
    }

    assert not collisions, (
        "requirements-dev.txt pins versions Home Assistant pins differently, "
        f"so the two files cannot be installed together: {collisions}"
    )
