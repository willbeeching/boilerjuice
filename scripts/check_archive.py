#!/usr/bin/env python3
"""Check a built release archive before anything is published.

An archive missing a module installs broken, and one carrying a virtualenv,
a cache or a stray credentials file publishes something nobody meant to. Both
are cheap to catch here and expensive to catch after a release is out.

Usage: python scripts/check_archive.py boilerjuice.zip
"""

from __future__ import annotations

import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "custom_components" / "boilerjuice"

# Anything matching these must never reach a release.
FORBIDDEN_PARTS = {"__pycache__", ".venv", "venv", ".git", ".env"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".log", ".db"}


def main(argv: list[str]) -> int:
    """Compare the archive against the working tree and return an exit status."""
    if len(argv) < 2:
        print("usage: check_archive.py <archive.zip>")
        return 2

    archive = pathlib.Path(argv[1])
    with zipfile.ZipFile(archive) as bundle:
        names = [name for name in bundle.namelist() if not name.endswith("/")]
        bad_zip = bundle.testzip()

    if bad_zip is not None:
        print(f"FAIL: {archive} is corrupt at {bad_zip}")
        return 1

    packaged = {name.removeprefix("boilerjuice/") for name in names}
    expected = {
        str(path.relative_to(SOURCE))
        for path in SOURCE.rglob("*")
        if path.is_file()
        and not set(path.parts) & FORBIDDEN_PARTS
        and path.suffix not in FORBIDDEN_SUFFIXES
    }

    if missing := sorted(expected - packaged):
        print("FAIL: the archive is missing files that are in the repository:")
        for name in missing:
            print(f"  {name}")
        return 1

    if extra := sorted(packaged - expected):
        print("FAIL: the archive contains files that are not in the repository:")
        for name in extra:
            print(f"  {name}")
        return 1

    for name in sorted(packaged):
        parts = set(pathlib.PurePosixPath(name).parts)
        if parts & FORBIDDEN_PARTS or pathlib.PurePosixPath(name).suffix in (
            FORBIDDEN_SUFFIXES
        ):
            print(f"FAIL: the archive contains {name}, which must never ship")
            return 1

    # Every archive has to be installable: the manifest is what Home Assistant
    # reads first, so a malformed one breaks the install before anything else.
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    for required in ("domain", "name", "version", "documentation", "codeowners"):
        if required not in manifest:
            print(f"FAIL: the manifest has no {required!r}")
            return 1

    for required in ("__init__.py", "manifest.json", "translations/en.json"):
        if required not in packaged:
            print(f"FAIL: the archive has no {required}")
            return 1

    print(f"{archive} contains {len(packaged)} files and matches the repository.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
