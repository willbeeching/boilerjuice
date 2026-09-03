"""Stored consumption must survive restarts and refuse to poison the totals."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.boilerjuice.coordinator import (
    STORAGE_KEY,
    STORAGE_VERSION,
    BoilerJuiceDataUpdateCoordinator,
)

from .helpers import make_entry, mock_site, tank_page

STORE = f"{STORAGE_VERSION}.{STORAGE_KEY}"


def stored(hass_storage, key: str) -> dict:
    return hass_storage[STORAGE_KEY]["data"][key]


async def test_a_restart_resumes_from_the_stored_totals(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    entry = make_entry(hass)
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {
            entry.entry_id: {
                "total_consumption_liters": 340.0,
                "total_consumption_kwh": 3519.0,
                "daily_consumption_liters": 12.0,
                "reference_volume": 2000.0,
                "reference_level": 80.0,
                "consumption_history": [12.0, 12.0],
                "consumption_history_with_dates": [],
                "last_update": "2026-01-01T00:00:00+00:00",
            }
        },
    }
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=79, litres=1950))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # 340 carried over, plus the 50 L drop seen on this poll.
        assert coordinator.total_consumption_usable_liters == 390.0
    finally:
        await coordinator.async_close()


async def test_two_accounts_do_not_overwrite_each_others_totals(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    first = make_entry(hass, email="one@example.com", tank_id="111111")
    second = make_entry(hass, email="two@example.com", tank_id="222222")

    one = BoilerJuiceDataUpdateCoordinator(hass, first)
    two = BoilerJuiceDataUpdateCoordinator(hass, second)

    try:
        mock_site(
            aioclient_mock,
            tank_html=tank_page(percentage=80, litres=2000),
            tank_id="111111",
        )
        mock_site(
            aioclient_mock,
            tank_html=tank_page(percentage=50, litres=1250),
            tank_id="222222",
            clear=False,
        )
        await one.async_refresh()
        await two.async_refresh()
        await hass.async_block_till_done()

        assert stored(hass_storage, first.entry_id)["reference_volume"] == 2000
        assert stored(hass_storage, second.entry_id)["reference_volume"] == 1250
    finally:
        await one.async_close()
        await two.async_close()


async def test_a_legacy_tank_keyed_document_is_adopted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    """Users upgrading from a tank-keyed store must keep their history."""
    entry = make_entry(hass)
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {
            "123456": {
                "total_consumption_liters": 120.0,
                "total_consumption_kwh": 1242.0,
                "daily_consumption_liters": 8.0,
                "reference_volume": 2000.0,
                "reference_level": 80.0,
                "consumption_history": [8.0],
                "consumption_history_with_dates": [],
            }
        },
    }
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.total_consumption_usable_liters == 120.0
        # And it has been rehomed under the entry id.
        assert entry.entry_id in hass_storage[STORAGE_KEY]["data"]
        assert "123456" not in hass_storage[STORAGE_KEY]["data"]
    finally:
        await coordinator.async_close()


async def test_a_legacy_default_bucket_is_only_adopted_with_a_tank_id(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    """With no tank id the shared bucket is ambiguous, so it is left alone."""
    entry = make_entry(hass, tank_id=None)
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {"default": {"total_consumption_liters": 500.0}},
    }
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.total_consumption_usable_liters == 0.0
        assert hass_storage[STORAGE_KEY]["data"]["default"] == {
            "total_consumption_liters": 500.0
        }
    finally:
        await coordinator.async_close()


async def test_reset_consumption_clears_the_stored_document(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    entry = make_entry(hass)
    coordinator = BoilerJuiceDataUpdateCoordinator(hass, entry)

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        mock_site(aioclient_mock, tank_html=tank_page(percentage=70, litres=1750))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.total_consumption_usable_liters == 250.0

        coordinator.reset_consumption()
        await hass.async_block_till_done()

        assert coordinator.total_consumption_usable_liters == 0.0
        document = stored(hass_storage, entry.entry_id)
        assert document["total_consumption_liters"] == 0.0
        assert document["reference_volume"] is None
        assert document["consumption_history_with_dates"] == []
    finally:
        await coordinator.async_close()
