"""One account, several tanks: discovery, removal and per-tank targeting."""

from __future__ import annotations

import pytest
from custom_components.boilerjuice import SERVICE_RESET_CONSUMPTION
from custom_components.boilerjuice.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TANK_ID,
    CONF_TANKS,
    DOMAIN,
    LOGIN_URL,
    PRICE_URL,
    TANKS_URL,
)
from custom_components.boilerjuice.coordinator import MISSING_LISTINGS_BEFORE_REMOVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import (
    PRICE_PAGE,
    SIGNED_IN_PAGE,
    coordinator_of,
    load_fixture,
    tank_device,
    tank_page,
    tracker_of,
)

FIRST = "123456"
SECOND = "789012"

ONE_TANK = f'<a href="/uk/users/tanks/{FIRST}/edit">One</a>'
TWO_TANKS = ONE_TANK + f'<a href="/uk/users/tanks/{SECOND}/edit">Two</a>'


def mock_account(
    aioclient_mock: AiohttpClientMocker, listing: str, *, clear: bool = True
) -> None:
    """Register an account whose tanks page lists `listing`."""
    if clear:
        aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=f"<html><body>{listing}</body></html>")
    aioclient_mock.get(
        f"{TANKS_URL}/{FIRST}/edit", text=tank_page(percentage=80, litres=2000)
    )
    aioclient_mock.get(
        f"{TANKS_URL}/{SECOND}/edit", text=tank_page(percentage=40, litres=900)
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)


@pytest.fixture
async def account(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> MockConfigEntry:
    """Return an unpinned account that starts out with two tanks."""
    mock_account(aioclient_mock, TWO_TANKS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_for(hass: HomeAssistant, tank_id: str, key: str) -> str:
    """Return the entity id of one tank's sensor."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{tank_id}_{key}")
    assert entity_id is not None, f"no {key} entity for tank {tank_id}"
    return entity_id


async def test_both_tanks_get_their_own_device_and_entities(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    coordinator = coordinator_of(account)
    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]

    assert tank_device(hass, account, FIRST) is not None
    assert tank_device(hass, account, SECOND) is not None

    # 14 sensors per tank, and nothing shared between them.
    assert len(hass.states.async_all("sensor")) == 28


async def test_each_tank_keeps_its_own_consumption(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    coordinator = coordinator_of(account)

    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=f"<html><body>{TWO_TANKS}</body></html>")
    aioclient_mock.get(
        f"{TANKS_URL}/{FIRST}/edit", text=tank_page(percentage=79, litres=1950)
    )
    aioclient_mock.get(
        f"{TANKS_URL}/{SECOND}/edit", text=tank_page(percentage=40, litres=900)
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert tracker_of(coordinator, FIRST).total_litres == 50.0
    assert tracker_of(coordinator, SECOND).total_litres == 0.0


async def test_a_tank_added_later_gets_entities_without_a_restart(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_account(aioclient_mock, ONE_TANK)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(hass.states.async_all("sensor")) == 14

    mock_account(aioclient_mock, TWO_TANKS)
    await coordinator_of(entry).async_refresh()
    await hass.async_block_till_done()

    assert len(hass.states.async_all("sensor")) == 28


async def test_a_tank_is_only_removed_after_repeated_authoritative_absences(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    coordinator = coordinator_of(account)
    mock_account(aioclient_mock, ONE_TANK)

    for poll in range(1, MISSING_LISTINGS_BEFORE_REMOVAL):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert SECOND in coordinator.tank_ids, f"removed after only {poll} listings"

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.tank_ids == [FIRST]


async def test_a_tank_that_comes_back_is_not_removed(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    coordinator = coordinator_of(account)

    mock_account(aioclient_mock, ONE_TANK)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    mock_account(aioclient_mock, TWO_TANKS)
    for _ in range(MISSING_LISTINGS_BEFORE_REMOVAL + 1):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]


async def test_a_failed_listing_never_removes_a_tank(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An outage must not delete anybody's devices."""
    coordinator = coordinator_of(account)

    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, status=503, text="")

    for _ in range(MISSING_LISTINGS_BEFORE_REMOVAL + 2):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]
    assert tank_device(hass, account, SECOND) is not None


async def test_only_the_included_tanks_are_tracked(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_account(aioclient_mock, TWO_TANKS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        options={CONF_TANKS: [SECOND]},
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert coordinator_of(entry).tank_ids == [SECOND]


async def test_a_device_target_resets_only_that_tank(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    coordinator = coordinator_of(account)
    tracker_of(coordinator, FIRST).state.total_litres = 40.0
    tracker_of(coordinator, SECOND).state.total_litres = 90.0

    device = tank_device(hass, account, FIRST)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"device_id": device.id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert tracker_of(coordinator, FIRST).total_litres == 0.0
    assert tracker_of(coordinator, SECOND).total_litres == 90.0


async def test_an_entry_target_resets_every_tank_on_the_account(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    coordinator = coordinator_of(account)
    tracker_of(coordinator, FIRST).state.total_litres = 40.0
    tracker_of(coordinator, SECOND).state.total_litres = 90.0

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"entry_id": account.entry_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert tracker_of(coordinator, FIRST).total_litres == 0.0
    assert tracker_of(coordinator, SECOND).total_litres == 0.0


async def test_excluding_a_tank_removes_its_device_immediately(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Filtering used to apply only to fetching, so the tank never left.

    Excluding a tank is a deliberate choice made just now, not an absence to
    wait out, so it takes effect on the next poll rather than after three.
    """
    coordinator = coordinator_of(account)
    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]

    hass.config_entries.async_update_entry(account, options={CONF_TANKS: [SECOND]})
    await hass.async_block_till_done()

    assert coordinator_of(account).tank_ids == [SECOND]
    assert tank_device(hass, account, FIRST) is None
    assert tank_device(hass, account, SECOND) is not None


async def test_excluding_a_tank_keeps_its_history_for_when_it_returns(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A tank BoilerJuice still lists has not gone away; the user hid it."""
    coordinator = coordinator_of(account)
    await coordinator.async_set_consumption(40.0, tank_id=FIRST)
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry(account, options={CONF_TANKS: [SECOND]})
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry(account, options={CONF_TANKS: []})
    await hass.async_block_till_done()

    assert tracker_of(coordinator_of(account), FIRST).total_litres == 40.0


async def test_pinning_one_tank_removes_the_other(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    coordinator = coordinator_of(account)
    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]

    hass.config_entries.async_update_entry(
        account, data={**account.data, CONF_TANK_ID: SECOND}
    )
    await hass.async_block_till_done()

    assert coordinator_of(account).tank_ids == [SECOND]


async def test_a_tank_that_fails_goes_unavailable_while_the_other_updates(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A failed tank must not sit there showing a reading that looks current."""
    coordinator = coordinator_of(account)

    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=f"<html><body>{TWO_TANKS}</body></html>")
    aioclient_mock.get(
        f"{TANKS_URL}/{FIRST}/edit", text=tank_page(percentage=70, litres=1750)
    )
    aioclient_mock.get(f"{TANKS_URL}/{SECOND}/edit", status=503, text="")
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert coordinator.reading(FIRST)["total_level_percentage"] == 70
    assert coordinator.reading(SECOND) is None

    healthy = hass.states.get(_entity_for(hass, FIRST, "oil_level"))
    broken = hass.states.get(_entity_for(hass, SECOND, "oil_level"))
    assert healthy.state == "70.0"
    assert broken.state == "unavailable"

    # The failed tank keeps its history, so nothing is lost when it returns.
    assert tracker_of(coordinator, SECOND) is not None


async def test_a_recovered_tank_becomes_available_again(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    coordinator = coordinator_of(account)

    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=f"<html><body>{TWO_TANKS}</body></html>")
    aioclient_mock.get(
        f"{TANKS_URL}/{FIRST}/edit", text=tank_page(percentage=70, litres=1750)
    )
    aioclient_mock.get(f"{TANKS_URL}/{SECOND}/edit", status=503, text="")
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    mock_account(aioclient_mock, TWO_TANKS)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.reading(SECOND) is not None
    assert hass.states.get(_entity_for(hass, SECOND, "oil_level")).state == "40.0"


async def test_one_broken_tank_raises_the_repair_despite_a_healthy_sibling(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A healthy tank used to clear the shared counter every single poll."""
    from custom_components.boilerjuice.coordinator import (
        PARSE_FAILURES_BEFORE_REPAIR,
    )
    from homeassistant.helpers import issue_registry as ir

    coordinator = coordinator_of(account)
    issue_id = f"page_layout_changed_{account.entry_id}"

    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=f"<html><body>{TWO_TANKS}</body></html>")
    aioclient_mock.get(
        f"{TANKS_URL}/{FIRST}/edit", text=tank_page(percentage=70, litres=1750)
    )
    aioclient_mock.get(
        f"{TANKS_URL}/{SECOND}/edit", text=load_fixture("tank_redesigned.html")
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    for _ in range(PARSE_FAILURES_BEFORE_REPAIR):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    mock_account(aioclient_mock, TWO_TANKS)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_a_broken_tank_warns_once_not_every_hour(
    hass: HomeAssistant,
    account: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hourly polling against a week-long outage would write 168 warnings."""
    coordinator = coordinator_of(account)

    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(TANKS_URL, text=f"<html><body>{TWO_TANKS}</body></html>")
    aioclient_mock.get(
        f"{TANKS_URL}/{FIRST}/edit", text=tank_page(percentage=70, litres=1750)
    )
    aioclient_mock.get(f"{TANKS_URL}/{SECOND}/edit", status=503, text="")
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    caplog.clear()
    with caplog.at_level("WARNING"):
        for _ in range(5):
            await coordinator.async_refresh()
            await hass.async_block_till_done()

    warnings = [
        record
        for record in caplog.records
        if record.levelname == "WARNING" and "could not be read" in record.message
    ]
    assert len(warnings) == 1


async def test_a_label_on_one_tanks_entity_resets_only_that_tank(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    """A label on an entity used to resolve only as far as the account.

    The resolver then fell back to account-wide targeting and reset both
    tanks, which is exactly the silent fan-out these actions must not do.
    """
    from homeassistant.helpers import label_registry as lr

    coordinator = coordinator_of(account)
    tracker_of(coordinator, FIRST).state.total_litres = 40.0
    tracker_of(coordinator, SECOND).state.total_litres = 90.0

    label = lr.async_get(hass).async_create("Kitchen tank")
    er.async_get(hass).async_update_entity(
        _entity_for(hass, FIRST, "oil_level"), labels={label.label_id}
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"label_id": label.label_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert tracker_of(coordinator, FIRST).total_litres == 0.0
    assert tracker_of(coordinator, SECOND).total_litres == 90.0


async def test_an_entity_area_override_resets_only_that_tank(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    """An entity can sit in a different area from its device."""
    from homeassistant.helpers import area_registry as ar

    coordinator = coordinator_of(account)
    tracker_of(coordinator, FIRST).state.total_litres = 40.0
    tracker_of(coordinator, SECOND).state.total_litres = 90.0

    area = ar.async_get(hass).async_create("Boiler room")
    er.async_get(hass).async_update_entity(
        _entity_for(hass, SECOND, "oil_level"), area_id=area.id
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"area_id": area.id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert tracker_of(coordinator, FIRST).total_litres == 40.0
    assert tracker_of(coordinator, SECOND).total_litres == 0.0


async def test_a_target_that_names_no_tank_is_refused_not_broadened(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    """Reaching an account without naming a tank must never mean "all of them"."""
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import label_registry as lr

    coordinator = coordinator_of(account)
    tracker_of(coordinator, FIRST).state.total_litres = 40.0
    tracker_of(coordinator, SECOND).state.total_litres = 90.0

    label = lr.async_get(hass).async_create("Nothing of ours")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"label_id": label.label_id},
            blocking=True,
        )

    assert tracker_of(coordinator, FIRST).total_litres == 40.0
    assert tracker_of(coordinator, SECOND).total_litres == 90.0


async def test_a_selected_tank_that_vanishes_is_eventually_removed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A listing that selects nothing is still an authoritative listing.

    Raising on it skipped reconciliation entirely, so the vanished tank's
    absence was never counted and its device stayed for ever.
    """
    from custom_components.boilerjuice.coordinator import (
        MISSING_LISTINGS_BEFORE_REMOVAL,
    )

    mock_account(aioclient_mock, TWO_TANKS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        options={CONF_TANKS: [FIRST]},
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = coordinator_of(entry)
    assert coordinator.tank_ids == [FIRST]

    # BoilerJuice stops listing the selected tank, but still lists the other.
    mock_account(aioclient_mock, ONE_TANK.replace(FIRST, SECOND))

    for poll in range(1, MISSING_LISTINGS_BEFORE_REMOVAL):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.tank_ids == [FIRST], f"removed after only {poll}"

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.tank_ids == []
    assert tank_device(hass, entry, FIRST) is None
    # And the entry says so rather than sitting there looking healthy.
    assert not coordinator.last_update_success


async def test_unloading_two_accounts_at_once_is_clean(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Both unloads must succeed; neither may trip over the other's teardown."""
    import asyncio

    from .helpers import setup_account

    first = await setup_account(
        hass, aioclient_mock, email="one@example.com", tank_id="111111"
    )
    second = await setup_account(
        hass, aioclient_mock, email="two@example.com", tank_id="222222"
    )

    results = await asyncio.gather(
        hass.config_entries.async_unload(first.entry_id),
        hass.config_entries.async_unload(second.entry_id),
        return_exceptions=True,
    )

    assert results == [True, True]
    assert not hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION)


async def test_a_redesigned_listing_page_cannot_erase_the_history(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An authenticated page we do not recognise is not proof of anything.

    It used to parse to "no tanks", which the coordinator acted on: after
    three polls both tanks and all their consumption history were gone, and
    no repair was raised because an empty list looked like a clean parse.
    """
    from custom_components.boilerjuice.coordinator import (
        PARSE_FAILURES_BEFORE_REPAIR,
    )
    from homeassistant.helpers import issue_registry as ir

    coordinator = coordinator_of(account)
    await coordinator.async_set_consumption(40.0, tank_id=FIRST)
    await hass.async_block_till_done()

    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    # Signed in, HTTP 200, and nothing we recognise.
    aioclient_mock.get(
        TANKS_URL,
        text="<html><body><h1>Your tanks</h1><div id='app'></div></body></html>",
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    for _ in range(PARSE_FAILURES_BEFORE_REPAIR + 2):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not coordinator.last_update_success
    # Both tanks, and the history, are untouched.
    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]
    assert tracker_of(coordinator, FIRST).total_litres == 40.0
    assert tank_device(hass, account, FIRST) is not None
    # And the user is told the site changed, rather than left guessing.
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"page_layout_changed_{account.entry_id}"
        )
        is not None
    )


async def test_a_removed_tank_keeps_its_history_and_resumes_it(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Retire, do not erase: a scraped absence must not delete user data."""
    from custom_components.boilerjuice.coordinator import (
        MISSING_LISTINGS_BEFORE_REMOVAL,
    )

    coordinator = coordinator_of(account)
    await coordinator.async_set_consumption(90.0, tank_id=SECOND)
    await hass.async_block_till_done()

    mock_account(aioclient_mock, ONE_TANK)
    for _ in range(MISSING_LISTINGS_BEFORE_REMOVAL):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.tank_ids == [FIRST]
    assert tank_device(hass, account, SECOND) is None
    # Retired, not erased.
    assert SECOND in coordinator._account.retired
    assert coordinator._account.tanks[SECOND].total_litres == 90.0

    # It comes back, and picks up where it left off.
    mock_account(aioclient_mock, TWO_TANKS)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]
    assert tracker_of(coordinator, SECOND).total_litres == 90.0
    assert SECOND not in coordinator._account.retired


async def test_a_retired_tank_is_not_resurrected_by_a_restart(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Keeping the history must not mean re-creating the device on reload."""
    from custom_components.boilerjuice.coordinator import (
        MISSING_LISTINGS_BEFORE_REMOVAL,
    )

    coordinator = coordinator_of(account)
    mock_account(aioclient_mock, ONE_TANK)
    for _ in range(MISSING_LISTINGS_BEFORE_REMOVAL):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert coordinator.tank_ids == [FIRST]

    await hass.config_entries.async_reload(account.entry_id)
    await hass.async_block_till_done()

    assert coordinator_of(account).tank_ids == [FIRST]
    assert tank_device(hass, account, SECOND) is None


async def test_a_redesigned_populated_listing_does_not_retire_the_tanks(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An "Add another tank" control is not evidence the account is empty.

    A populated page whose tank markup changed used to parse as empty, so
    the devices were retired after three polls and no layout repair was
    raised: the integration silently disappeared.
    """
    from custom_components.boilerjuice.coordinator import (
        MISSING_LISTINGS_BEFORE_REMOVAL,
        PARSE_FAILURES_BEFORE_REPAIR,
    )
    from homeassistant.helpers import issue_registry as ir

    coordinator = coordinator_of(account)

    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(
        TANKS_URL,
        text=(
            "<html><body><h1>Your tanks</h1>"
            f'<div class="tank-card" data-tank="{FIRST}"><h2>Garden</h2></div>'
            f'<div class="tank-card" data-tank="{SECOND}"><h2>Barn</h2></div>'
            '<a href="/uk/users/tanks/new">Add another tank</a>'
            "</body></html>"
        ),
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    for _ in range(
        max(MISSING_LISTINGS_BEFORE_REMOVAL, PARSE_FAILURES_BEFORE_REPAIR) + 1
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]
    assert tank_device(hass, account, FIRST) is not None
    assert tank_device(hass, account, SECOND) is not None
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"page_layout_changed_{account.entry_id}"
        )
        is not None
    )
