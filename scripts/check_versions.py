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
CURRENT_REQUIREMENTS = ROOT / "requirements-test.txt"
CI = ROOT / ".github" / "workflows" / "ci.yaml"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
TAG = re.compile(r"^v\d+\.\d+\.\d+$")


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

    if not QUALITY_SCALE.exists():
        fail("quality_scale.yaml is missing")

    claimed = manifest.get("quality_scale")
    if claimed and claimed not in {"no_score", "bronze", "silver", "gold", "platinum"}:
        fail(f"unknown quality scale {claimed!r}")
    print(f"  quality scale: {claimed}")

    if len(argv) > 1:
        tag = argv[1].removeprefix("refs/tags/")
        if not TAG.match(tag):
            fail(f"tag {tag!r} is not vMAJOR.MINOR.PATCH")
        if tag.removeprefix("v") != version:
            fail(f"tag {tag} does not match manifest version {version}")
        print(f"  tag {tag} matches the manifest")

    print("Versions agree.")
    return 0


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
