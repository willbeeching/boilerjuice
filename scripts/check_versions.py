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

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "boilerjuice" / "manifest.json"
HACS = ROOT / "hacs.json"
QUALITY_SCALE = ROOT / "quality_scale.yaml"
MIN_REQUIREMENTS = ROOT / "requirements-test-min.txt"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


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
    tested = _minimum_lane_version()
    if tested != floor:
        fail(
            f"hacs.json declares Home Assistant {floor} but the minimum test "
            f"lane runs {tested}"
        )
    print(f"  minimum lane runs: {tested}")

    if not QUALITY_SCALE.exists():
        fail("quality_scale.yaml is missing")

    claimed = manifest.get("quality_scale")
    if claimed and claimed not in {"no_score", "bronze", "silver", "gold", "platinum"}:
        fail(f"unknown quality scale {claimed!r}")
    print(f"  quality scale: {claimed}")

    if len(argv) > 1:
        tag = argv[1].removeprefix("refs/tags/")
        if tag.removeprefix("v") != version:
            fail(f"tag {tag} does not match manifest version {version}")
        print(f"  tag {tag} matches the manifest")

    print("Versions agree.")
    return 0


def _minimum_lane_version() -> str:
    """Return the Home Assistant version the minimum test lane installs."""
    for line in MIN_REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        if match := re.fullmatch(r"#\s*home-assistant:\s*(\S+)", line.strip()):
            return match.group(1)
    fail(
        "requirements-test-min.txt has no '# home-assistant: <version>' line, "
        "so the declared floor cannot be checked against what CI runs"
    )
    raise AssertionError  # unreachable; fail() exits


if __name__ == "__main__":
    sys.exit(main(sys.argv))
