"""Diagnostics get pasted into public bug reports, so they must not leak."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from custom_components.boilerjuice.diagnostics import async_get_config_entry_diagnostics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import (
    coordinator_of,
    load_fixture,
    mock_site,
    setup_account,
    tank_page,
    tracker_of,
)

# Everything a BoilerJuice bug report must never carry.
SECRETS = (
    "someone@example.com",
    "hunter2",
    "123456",
    "Garden Tank",
    "csrf",
    "authenticity_token",
    "<html",
)


async def test_diagnostics_describe_the_scrape_without_the_account(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_site(aioclient_mock, tank_html=load_fixture("tank_current.html"))
    entry = await setup_account(hass, aioclient_mock)

    report = await async_get_config_entry_diagnostics(hass, entry)
    serialised = json.dumps(report)

    for secret in SECRETS:
        assert secret not in serialised, f"diagnostics leaked {secret!r}"

    assert report["update_health"]["last_update_success"] is True
    assert report["update_health"]["tank_count"] == 1
    assert report["integration"]["storage_version"] == 2

    tank = report["tanks"][0]
    assert tank["tank"] == "tank_1"
    assert tank["parsed_fields"]["total_level_percentage"] is True
    assert tank["parsed_fields"]["capacity_litres"] is True
    assert tank["history_rows"] == 0


async def test_diagnostics_show_which_fields_stopped_parsing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The point of the report: which field the site stopped serving."""
    entry = await setup_account(hass, aioclient_mock)

    from .helpers import tank_page

    mock_site(aioclient_mock, tank_html=tank_page(percentage=80))
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    report = await async_get_config_entry_diagnostics(hass, entry)
    fields = report["tanks"][0]["parsed_fields"]

    assert fields["total_level_percentage"] is True
    assert fields["usable_volume_litres"] is False


async def test_diagnostics_report_a_failing_update(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)

    mock_site(aioclient_mock, tank_html=load_fixture("tank_redesigned.html"))
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["update_health"]["last_update_success"] is False
    assert report["update_health"]["last_exception_type"] == "BoilerJuiceParseError"
    assert report["update_health"]["failing_scopes"] == 1
    assert report["update_health"]["worst_parse_failure_run"] == 1


# --- logs -----------------------------------------------------------------


async def test_no_log_record_carries_the_tank_id(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A scraper's logs end up in bug reports.

    aiohttp's own exception text includes the request URL, and a tank page
    URL is `/tanks/<tank id>/edit`, so passing that text through to a warning
    put the tank id in the log despite the privacy work everywhere else.

    Checks the formatted record, not just the message, so an exception
    attached with exc_info would fail this too.
    """
    import logging

    import aiohttp
    import yarl
    from multidict import CIMultiDict, CIMultiDictProxy

    from .helpers import TANK_ID, TANK_URL

    entry = await setup_account(hass, aioclient_mock)
    coordinator = entry.runtime_data.coordinator

    mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
    aioclient_mock.clear_requests()
    from custom_components.boilerjuice.const import LOGIN_URL, PRICE_URL, TANKS_URL

    from .helpers import SIGNED_IN_PAGE

    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list.html"))
    aioclient_mock.get(PRICE_URL, text="")
    aioclient_mock.get(
        TANK_URL,
        exc=aiohttp.ClientResponseError(
            aiohttp.RequestInfo(
                yarl.URL(TANK_URL), "GET", CIMultiDictProxy(CIMultiDict())
            ),
            (),
            status=502,
            message="Bad Gateway",
        ),
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="custom_components.boilerjuice"):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not coordinator.last_update_success

    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    for record in caplog.records:
        rendered = formatter.format(record)
        assert TANK_ID not in rendered, f"tank id leaked into: {rendered}"
        assert "hunter2" not in rendered
        assert "someone@example.com" not in rendered


async def test_diagnostics_show_where_the_history_has_a_gap(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A row count alone cannot tell a lost season from an unstarted one.

    Autumn and winter rows missing while summer is full is a reset that
    took a heating season. Autumn missing on 4 September is just autumn
    not having started. The per-month counts separate the two.
    """
    mock_site(aioclient_mock, tank_html=load_fixture("tank_current.html"))
    entry = await setup_account(hass, aioclient_mock)
    tracker = tracker_of(coordinator_of(entry))

    july = dt_util.now().replace(month=7, day=1)
    tracker.state.history = [
        (july + timedelta(days=offset), 1.5) for offset in range(0, 62, 2)
    ]

    report = await async_get_config_entry_diagnostics(hass, entry)
    tank = report["tanks"][0]

    assert tank["history_span"]["first"].endswith("-07-01")
    assert sorted(tank["history_rows_by_month"]) == [
        f"{july.year}-07",
        f"{july.year}-08",
    ]
    assert sum(tank["history_rows_by_month"].values()) == tank["history_rows"]
    assert json.dumps(report)  # still serialisable


async def test_diagnostics_report_no_span_without_history(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_site(aioclient_mock, tank_html=load_fixture("tank_current.html"))
    entry = await setup_account(hass, aioclient_mock)

    tank = (await async_get_config_entry_diagnostics(hass, entry))["tanks"][0]

    assert tank["history_span"] is None
    assert tank["history_rows_by_month"] == {}


async def test_diagnostics_work_while_setup_is_failing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The one moment anybody wants the download is when setup is stuck.

    Reading runtime_data off an entry that never finished setting up
    raised AttributeError, which Home Assistant served as HTTP 500.
    """
    mock_site(aioclient_mock, tank_html=load_fixture("tank_current.html"))
    entry = await setup_account(hass, aioclient_mock)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["data"]["update_health"]["set_up"] is False
    assert report["data"]["tanks"] == []
    assert json.dumps(report)

    for secret in SECRETS:
        assert secret not in json.dumps(report), f"diagnostics leaked {secret!r}"
