#!/usr/bin/env python3
"""Sign in to BoilerJuice and report what the integration can read.

This drives the integration's own client and parser rather than a second
copy of them, so what it reports is what Home Assistant would see: the same
sign-in, the same timeouts, the same refusal to follow a redirect off
boilerjuice.com with the password attached, and the same parser.

Output is redacted by default. It says which fields parsed, not what they
contain, so it can be pasted into a public issue. Pass --show-values when
you need the actual readings on your own screen; it prints a warning first.

    # from the current test-lane environment, which has Home Assistant
    .venv/bin/python scripts/check_live_account.py
    .venv/bin/python scripts/check_live_account.py --show-values

Credentials come from the environment, or a .env file (see .env.example):

    BOILERJUICE_EMAIL, BOILERJUICE_PASSWORD, BOILERJUICE_TANK_ID (optional)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import aiohttp
from custom_components.boilerjuice.client import (
    REQUEST_TIMEOUT,
    BoilerJuiceClient,
)
from custom_components.boilerjuice.errors import BoilerJuiceError
from custom_components.boilerjuice.models import TankReading

# Every field the parser looks for, in the order the page presents them.
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


def load_dotenv_if_present() -> None:
    """Read a .env file into the environment, without a dependency."""
    env = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def describe(reading: TankReading, show_values: bool) -> None:
    """Print which fields parsed, and optionally what they hold."""
    for field in FIELDS:
        value = getattr(reading, field)
        mark = "found   " if value is not None else "MISSING "
        if show_values and value is not None:
            print(f"  {mark} {field:20s} {value}")
        else:
            print(f"  {mark} {field}")


async def run(show_values: bool) -> int:
    """Sign in, read the tanks, and report. Returns an exit status."""
    load_dotenv_if_present()
    email = os.environ.get("BOILERJUICE_EMAIL")
    password = os.environ.get("BOILERJUICE_PASSWORD")
    if not email or not password:
        print("Set BOILERJUICE_EMAIL and BOILERJUICE_PASSWORD (see .env.example)")
        return 2

    pinned = os.environ.get("BOILERJUICE_TANK_ID") or None

    client = BoilerJuiceClient(
        lambda timeout: aiohttp.ClientSession(timeout=timeout),
        email,
        password,
    )
    print(f"Using the integration's client, timeout {REQUEST_TIMEOUT}")

    try:
        tank_ids = [pinned] if pinned else await client.async_list_tank_ids()
        print(f"\nSigned in. The account lists {len(tank_ids)} tank(s).")

        failures = 0
        for index, tank_id in enumerate(tank_ids, start=1):
            print(f"\nTank {index} of {len(tank_ids)}:")
            try:
                reading = await client.async_fetch_tank(tank_id)
            except BoilerJuiceError as err:
                failures += 1
                print(f"  COULD NOT READ: {type(err).__name__}: {err}")
                continue
            describe(reading, show_values)

        price = await client.async_fetch_price()
        print(f"\nOil price: {'found' if price is not None else 'MISSING'}")
        if show_values and price is not None:
            print(f"  {price} pence per litre")

        if failures:
            print(f"\n{failures} tank(s) could not be read.")
            return 1
        print("\nEverything the integration needs parsed cleanly.")
        return 0
    except BoilerJuiceError as err:
        # The message carries no credentials, no CSRF token and no page body.
        print(f"\nFAILED: {type(err).__name__}: {err}")
        return 1
    finally:
        await client.async_close()


def main(argv: list[str]) -> int:
    """Parse the arguments and run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--show-values",
        action="store_true",
        help="print the readings themselves, not just which parsed",
    )
    args = parser.parse_args(argv[1:])

    if args.show_values:
        print(
            "WARNING: this prints your tank name, tank id and readings. "
            "Do not paste the output into a public issue.\n"
        )

    return asyncio.run(run(args.show_values))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
