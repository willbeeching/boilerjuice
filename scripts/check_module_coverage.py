#!/usr/bin/env python3
"""Fail if any integration module falls below the coverage floor.

`coverage --fail-under` only checks the total, which lets one well-covered
module hide another that is barely tested at all.

Usage: python scripts/check_module_coverage.py coverage.json [floor]
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    report_path = argv[1] if len(argv) > 1 else "coverage.json"
    floor = float(argv[2]) if len(argv) > 2 else 95.0

    with open(report_path, encoding="utf-8") as handle:
        report = json.load(handle)

    failures = []
    for path, data in sorted(report["files"].items()):
        percent = data["summary"]["percent_covered"]
        marker = " " if percent >= floor else "!"
        print(f"{marker} {percent:6.2f}%  {path}")
        if percent < floor:
            failures.append((path, percent))

    if failures:
        print(f"\n{len(failures)} module(s) below {floor:.0f}%:")
        for path, percent in failures:
            print(f"  {path}: {percent:.2f}%")
        return 1

    print(f"\nEvery module is at or above {floor:.0f}%.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
