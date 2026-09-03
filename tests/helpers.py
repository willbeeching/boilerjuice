"""Shared helpers for building a fake BoilerJuice site."""

from __future__ import annotations

import pathlib

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.boilerjuice.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TANK_ID,
    DOMAIN,
    LOGIN_URL,
    PRICE_URL,
    TANKS_URL,
)

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"

TANK_ID = "123456"
TANK_URL = f"{TANKS_URL}/{TANK_ID}/edit"
SIGNED_IN_PAGE = "<html><body><h1>Your account</h1></body></html>"
PRICE_PAGE = "<html><body><p>Today: 62.45 pence per litre</p></body></html>"


def load_fixture(name: str) -> str:
    """Return the contents of an HTML fixture."""
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def tank_page(percentage: float | None = None, litres: int | None = None) -> str:
    """Build a tank page carrying the given level and/or volume."""
    parts = ['<html><body><input id="tank_size" value="2500">']
    if percentage is not None:
        parts.append(
            f'<div id="usable-oil"><div class="oil-level" '
            f'data-percentage="{percentage}"></div></div>'
        )
    if litres is not None:
        parts.append(f"<p>{litres} litres of oil</p>")
    parts.append("</body></html>")
    return "".join(parts)


def mock_site(
    aioclient_mock: AiohttpClientMocker,
    *,
    tank_html: str,
    tank_id: str = TANK_ID,
    price_html: str | None = PRICE_PAGE,
    login_html: str = SIGNED_IN_PAGE,
    clear: bool = True,
) -> None:
    """Register a full, successful BoilerJuice round trip."""
    if clear:
        aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=login_html)
    aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list.html"))
    aioclient_mock.get(f"{TANKS_URL}/{tank_id}/edit", text=tank_html)
    if price_html is None:
        aioclient_mock.get(PRICE_URL, status=503, text="")
    else:
        aioclient_mock.get(PRICE_URL, text=price_html)


def make_entry(
    hass: HomeAssistant,
    *,
    email: str = "someone@example.com",
    tank_id: str | None = TANK_ID,
    **extra,
) -> MockConfigEntry:
    """Return a config entry added to hass."""
    data = {CONF_EMAIL: email, CONF_PASSWORD: "hunter2", **extra}
    if tank_id is not None:
        data[CONF_TANK_ID] = tank_id
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=email)
    entry.add_to_hass(hass)
    return entry


async def setup_account(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    *,
    email: str = "someone@example.com",
    tank_id: str = TANK_ID,
    litres: int = 2000,
    percentage: float = 80,
) -> MockConfigEntry:
    """Set up one fully-loaded BoilerJuice account."""
    mock_site(
        aioclient_mock,
        tank_html=tank_page(percentage=percentage, litres=litres),
        tank_id=tank_id,
        clear=False,
    )
    entry = make_entry(hass, email=email, tank_id=tank_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
