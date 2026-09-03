"""The branches only reached by malformed input or unusual accounts.

These carry real behaviour: refusing bad stored data, refusing an
unresolvable target, and picking a tank when the account has several.
"""

from __future__ import annotations

import pytest
from custom_components.boilerjuice import SERVICE_RESET_CONSUMPTION
from custom_components.boilerjuice.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
    LOGIN_URL,
    PRICE_URL,
    TANKS_URL,
)
from custom_components.boilerjuice.coordinator import BoilerJuiceDataUpdateCoordinator
from custom_components.boilerjuice.parser import parse_tank_page
from custom_components.boilerjuice.storage import (
    AccountState,
    ConsumptionStore,
    InvalidStoredData,
    account_from_document,
    state_from_document,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import (
    PRICE_PAGE,
    SIGNED_IN_PAGE,
    TANK_URL,
    coordinator_of,
    load_fixture,
    make_entry,
    mock_site,
    setup_account,
    tank_page,
    tracker_of,
)

# --- stored data ----------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        pytest.param("not an object", id="account-not-an-object"),
        pytest.param({"tanks": "some"}, id="tanks-not-an-object"),
        pytest.param({"missing": []}, id="missing-not-an-object"),
        pytest.param({"tanks": {"1": {"total_litres": "lots"}}}, id="bad-tank"),
        pytest.param({"unassigned": {"total_litres": -1}}, id="bad-unassigned"),
    ],
)
def test_an_untrustworthy_account_document_is_refused(document: object) -> None:
    with pytest.raises(InvalidStoredData):
        account_from_document(document)


def test_a_non_string_timestamp_is_refused() -> None:
    with pytest.raises(InvalidStoredData):
        state_from_document({"last_update": 1234567890})


def test_a_store_reports_its_key(hass: HomeAssistant) -> None:
    entry = make_entry(hass)
    store = ConsumptionStore(hass, entry.entry_id, "123456")

    assert store.key == f"{DOMAIN}.{entry.entry_id}"


def test_an_empty_account_state_round_trips() -> None:
    from custom_components.boilerjuice.storage import document_from_account

    assert (
        account_from_document(document_from_account(AccountState())) == AccountState()
    )


# --- parsing --------------------------------------------------------------


def test_a_multi_valued_attribute_is_read_as_one_string() -> None:
    """Beautiful Soup returns a list for attributes it knows are multi-valued."""
    html = (
        '<div id="usable-oil"><div class="oil-level a b" data-percentage="50">'
        "</div></div>"
    )
    reading = parse_tank_page(html, "123456")

    assert reading.level_percentage == 50


def test_a_tank_link_without_a_usable_id_is_skipped() -> None:
    """The empty state still has to be recognisable for the answer to count."""
    from custom_components.boilerjuice.parser import parse_tank_ids

    html = (
        '<a href="/uk/users/tanks/notanumber/edit">Nope</a>'
        '<a href="/uk/users/tanks/new">Add a tank</a>'
    )

    assert parse_tank_ids(html) == []


def test_an_oil_type_select_with_nothing_selected() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        '<select id="tank_oil_type_id"><option value="1">Kerosene</option></select>'
    )
    reading = parse_tank_page(html, "123456")

    assert reading.oil_type is None


def test_a_volume_phrase_that_does_not_parse_is_skipped() -> None:
    html = (
        '<div id="usable-oil"><div class="oil-level" data-percentage="50"></div></div>'
        "<p>many litres of oil</p><p>900 litres of oil</p>"
    )
    reading = parse_tank_page(html, "123456")

    assert reading.volume_litres == 900


# --- accounts and targets -------------------------------------------------


async def test_an_account_with_several_tanks_and_no_preference_uses_them_all(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list_multiple.html"))
    aioclient_mock.get(TANK_URL, text=tank_page(percentage=80, litres=2000))
    aioclient_mock.get(
        f"{TANKS_URL}/789012/edit", text=tank_page(percentage=40, litres=900)
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert sorted(coordinator_of(entry).tank_ids) == ["123456", "789012"]


async def test_an_unknown_area_target_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)
    tracker_of(coordinator_of(entry)).state.total_litres = 40.0

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"area_id": "nowhere"},
            blocking=True,
        )

    assert tracker_of(coordinator_of(entry)).total_litres == 40.0


async def test_an_unexpected_listing_error_becomes_an_update_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    from unittest.mock import patch

    from homeassistant.helpers.update_coordinator import UpdateFailed

    coordinator = BoilerJuiceDataUpdateCoordinator(hass, make_entry(hass, tank_id=None))

    try:
        mock_site(aioclient_mock, tank_html=tank_page(percentage=80, litres=2000))
        with patch.object(
            coordinator._client,
            "async_list_tank_ids",
            side_effect=ValueError("something unforeseen"),
        ):
            await coordinator.async_refresh()

        assert isinstance(coordinator.last_exception, UpdateFailed)
    finally:
        await coordinator.async_close()


async def test_migrated_history_is_dropped_when_the_tank_is_ambiguous(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
) -> None:
    """v1 recorded no tank id, so with several tanks it cannot be attributed."""
    from custom_components.boilerjuice.storage import (
        LEGACY_STORAGE_KEY,
        LEGACY_STORAGE_VERSION,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)
    hass_storage[LEGACY_STORAGE_KEY] = {
        "version": LEGACY_STORAGE_VERSION,
        "data": {entry.entry_id: {"total_consumption_liters": 340.0}},
    }

    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=load_fixture("tanks_list_multiple.html"))
    aioclient_mock.get(TANK_URL, text=tank_page(percentage=80, litres=2000))
    aioclient_mock.get(
        f"{TANKS_URL}/789012/edit", text=tank_page(percentage=40, litres=900)
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = coordinator_of(entry)
    assert tracker_of(coordinator, "123456").total_litres == 0.0
    assert tracker_of(coordinator, "789012").total_litres == 0.0


# --- the diagnostic scripts -----------------------------------------------


def test_the_saved_page_script_uses_the_real_parser(tmp_path) -> None:
    """A second copy of the parsing rules would drift out of step."""
    import sys

    sys.path.insert(0, "scripts")
    from check_saved_tank_page import main

    good = tmp_path / "good.html"
    good.write_text(load_fixture("tank_current.html"), encoding="utf-8")
    assert main(["check_saved_tank_page.py", str(good)]) == 0

    bad = tmp_path / "bad.html"
    bad.write_text(load_fixture("tank_redesigned.html"), encoding="utf-8")
    assert main(["check_saved_tank_page.py", str(bad)]) == 1


def test_the_saved_page_script_redacts_by_default(tmp_path, capsys) -> None:
    """Output is meant to be safe to paste into a public issue."""
    import sys

    sys.path.insert(0, "scripts")
    from check_saved_tank_page import main

    page = tmp_path / "page.html"
    page.write_text(load_fixture("tank_current.html"), encoding="utf-8")

    main(["check_saved_tank_page.py", str(page)])
    redacted = capsys.readouterr().out

    # The fixture's own values, none of which may appear.
    for secret in ("62.5", "1562", "Garden Tank", "Harlequin", "H2500T"):
        assert secret not in redacted, f"leaked {secret!r}"
    assert "found" in redacted

    main(["check_saved_tank_page.py", str(page), "--show-values"])
    shown = capsys.readouterr().out
    assert "Garden Tank" in shown
