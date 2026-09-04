#!/usr/bin/env python3
"""Run the integration's parser over a page you saved from BoilerJuice.

Uses the real parser, so what it reports is what Home Assistant would make
of the page: no second copy of the parsing rules to drift out of step.

Output is redacted by default, so it can be pasted into a public issue. The
saved page itself cannot: it contains your account details. Do not attach it.

    # from the current test-lane environment, which has Home Assistant
    .venv/bin/python scripts/check_saved_tank_page.py tank_page.html
    .venv/bin/python scripts/check_saved_tank_page.py tank_page.html --show-values
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from custom_components.boilerjuice.errors import BoilerJuiceError
from custom_components.boilerjuice.parser import parse_tank_page

FIELDS = (
    "level_percentage",
    "volume_litres",
    "capacity_litres",
    "height_cm",
    "name",
    "model",
    "model_id",
    "manufacturer",
    "shape",
    "oil_type",
)


def main(argv: list[str]) -> int:
    """Parse the saved page and report. Returns an exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page", help="an HTML file saved from the tank page")
    parser.add_argument("--tank-id", default="123456", help="any numeric id will do")
    parser.add_argument(
        "--show-values",
        action="store_true",
        help="print the readings themselves, not just which parsed",
    )
    args = parser.parse_args(argv[1:])

    html = pathlib.Path(args.page).read_text(encoding="utf-8", errors="replace")

    try:
        reading = parse_tank_page(html, args.tank_id)
    except BoilerJuiceError as err:
        print(f"The integration would refuse this page: {type(err).__name__}: {err}")
        return 1

    print("The integration would accept this page. Fields:")
    for field in FIELDS:
        value = getattr(reading, field)
        mark = "found   " if value is not None else "MISSING "
        if args.show_values and value is not None:
            print(f"  {mark} {field:20s} {value}")
        else:
            print(f"  {mark} {field}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
