"""One account, several tanks: discovery, removal and per-tank targeting."""

from __future__ import annotations

import pytest
from custom_components.boilerjuice import (
    SERVICE_RESET_CONSUMPTION,
    SERVICE_SET_CONSUMPTION,
)
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import (
    PRICE_PAGE,
    SIGNED_IN_PAGE,
    coordinator_of,
    load_fixture,
    reconfigure,
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
    assert len(hass.states.async_all("sensor")) == 30


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
    assert len(hass.states.async_all("sensor")) == 15

    mock_account(aioclient_mock, TWO_TANKS)
    await coordinator_of(entry).async_refresh()
    await hass.async_block_till_done()

    assert len(hass.states.async_all("sensor")) == 30


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

    await reconfigure(hass, account, options={CONF_TANKS: [SECOND]})

    assert coordinator_of(account).tank_ids == [SECOND]
    assert tank_device(hass, account, FIRST) is None
    assert tank_device(hass, account, SECOND) is not None


async def test_excluding_a_tank_keeps_its_history_for_when_it_returns(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A tank BoilerJuice still lists has not gone away; the user hid it."""
    coordinator = coordinator_of(account)
    await coordinator.async_set_consumption(40.0, tank_ids=[FIRST])
    await hass.async_block_till_done()

    await reconfigure(hass, account, options={CONF_TANKS: [SECOND]})
    await reconfigure(hass, account, options={CONF_TANKS: []})

    assert tracker_of(coordinator_of(account), FIRST).total_litres == 40.0


async def test_pinning_one_tank_removes_the_other(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    coordinator = coordinator_of(account)
    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]

    await reconfigure(hass, account, data={**account.data, CONF_TANK_ID: SECOND})

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
    # The actions outlive the entries that used them.
    assert hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION)


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
    await coordinator.async_set_consumption(40.0, tank_ids=[FIRST])
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
    await coordinator.async_set_consumption(90.0, tank_ids=[SECOND])
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


async def test_a_tank_added_after_every_previous_one_was_retired_gets_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The device appeared with nothing on it.

    The discovery callback only fired when the account already had a tank,
    so an account that had lost all of them never got entities for the next
    one it gained.
    """
    from custom_components.boilerjuice.coordinator import (
        MISSING_LISTINGS_BEFORE_REMOVAL,
    )

    mock_account(aioclient_mock, ONE_TANK)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "someone@example.com", CONF_PASSWORD: "hunter2"},
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = coordinator_of(entry)
    assert coordinator.tank_ids == [FIRST]

    # Every tank goes.
    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, text=load_fixture("login.html"))
    aioclient_mock.post(LOGIN_URL, text=SIGNED_IN_PAGE)
    aioclient_mock.get(
        TANKS_URL, text="<html><body><p>You have no tanks yet.</p></body></html>"
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    for _ in range(MISSING_LISTINGS_BEFORE_REMOVAL):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.tank_ids == []
    assert tank_device(hass, entry, FIRST) is None

    # A different tank appears.
    mock_account(aioclient_mock, ONE_TANK.replace(FIRST, SECOND))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.tank_ids == [SECOND]
    assert tank_device(hass, entry, SECOND) is not None
    # ...and it has its sensors, not just a bare device.
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{SECOND}_oil_level")
        is not None
    )
    assert hass.states.get(_entity_for(hass, SECOND, "oil_level")).state == "40.0"


async def test_removing_a_tank_uses_the_supported_registry_call(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """async_update_device(remove_config_entry_id=...) breaks in 2027.8."""
    from unittest.mock import patch

    from custom_components.boilerjuice.coordinator import (
        MISSING_LISTINGS_BEFORE_REMOVAL,
    )

    coordinator = coordinator_of(account)
    mock_account(aioclient_mock, ONE_TANK)

    registry = dr.async_get(hass)
    with patch.object(
        registry, "async_update_device", wraps=registry.async_update_device
    ) as updated:
        for _ in range(MISSING_LISTINGS_BEFORE_REMOVAL):
            await coordinator.async_refresh()
            await hass.async_block_till_done()

    assert coordinator.tank_ids == [FIRST]
    assert tank_device(hass, account, SECOND) is None
    assert not [
        call for call in updated.mock_calls if "remove_config_entry_id" in str(call)
    ]


async def test_a_status_line_split_by_inline_markup_does_not_retire_the_tanks(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A fragment inside a <strong> is still part of the sentence around it.

    The listing here is a populated page whose only unrecognised part is a
    footer reading "No tanks need a delivery today", with two of its words
    emphasised. Reading text nodes one at a time saw the fragment on its
    own, called the account empty, and retired both devices.
    """
    from custom_components.boilerjuice.coordinator import (
        MISSING_LISTINGS_BEFORE_REMOVAL,
        PARSE_FAILURES_BEFORE_REPAIR,
    )

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
            "<footer><p><strong>No tanks</strong> need a delivery today</p>"
            "<p>Good news: <b>no tanks</b> are low</p></footer>"
            "</body></html>"
        ),
    )
    aioclient_mock.get(PRICE_URL, text=PRICE_PAGE)

    for _ in range(
        max(MISSING_LISTINGS_BEFORE_REMOVAL, PARSE_FAILURES_BEFORE_REPAIR) + 1
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert sorted(coordinator.tank_ids) == [FIRST, SECOND]
    assert tank_device(hass, account, FIRST) is not None
    assert tank_device(hass, account, SECOND) is not None


async def test_setting_the_account_total_refuses_while_one_tank_is_offline(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    """An account-wide set is all tanks or none of them.

    The handler only asked whether the account had any data at all, so with
    one tank offline the other still took the new total. The offline tank
    got the total without a new reference, and booked the difference as
    consumption the moment it came back.
    """
    coordinator = coordinator_of(account)
    tracker_of(coordinator, FIRST).state.total_litres = 40.0
    tracker_of(coordinator, SECOND).state.total_litres = 90.0

    published = dict(coordinator.data)
    del published[SECOND]
    coordinator.async_set_updated_data(published)
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CONSUMPTION,
            {"liters": 1000.0, "entry_id": account.entry_id},
            blocking=True,
        )

    assert raised.value.translation_key == "tanks_without_readings"
    assert SECOND in (raised.value.translation_placeholders or {})["tanks"]
    assert tracker_of(coordinator, FIRST).total_litres == 40.0
    assert tracker_of(coordinator, SECOND).total_litres == 90.0


async def test_setting_one_tanks_total_still_works_while_the_other_is_offline(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    """Refusing the account-wide call must not refuse the specific one."""
    coordinator = coordinator_of(account)
    tracker_of(coordinator, SECOND).state.total_litres = 90.0

    published = dict(coordinator.data)
    del published[SECOND]
    coordinator.async_set_updated_data(published)
    await hass.async_block_till_done()

    device = tank_device(hass, account, FIRST)
    assert device is not None
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CONSUMPTION,
        {"liters": 1000.0, "device_id": device.id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert tracker_of(coordinator, FIRST).total_litres == 1000.0
    assert tracker_of(coordinator, SECOND).total_litres == 90.0


async def test_the_coordinator_refuses_the_same_call_on_its_own(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    """The invariant belongs to the coordinator, not only to the handler."""
    coordinator = coordinator_of(account)

    published = dict(coordinator.data)
    del published[SECOND]
    coordinator.async_set_updated_data(published)
    await hass.async_block_till_done()

    assert coordinator.tanks_without_readings() == [SECOND]
    assert coordinator.tanks_without_readings([FIRST]) == []

    with pytest.raises(HomeAssistantError) as raised:
        await coordinator.async_set_consumption(1000.0)

    assert raised.value.translation_key == "tanks_without_readings"
    assert tracker_of(coordinator, FIRST).total_litres == 0.0


async def test_the_snapshot_a_refresh_returns_is_built_under_the_lock(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    """Nothing may run between building the snapshot and returning it.

    Home Assistant assigns the returned snapshot to coordinator.data as soon
    as _async_update_data returns. An action publishing its own snapshot
    takes the same lock, so as long as the return happens inside the lock the
    two cannot interleave. Registering the devices is the last thing before
    the return, so it is where that is checked.
    """
    coordinator = coordinator_of(account)
    held: list[bool] = []
    register = coordinator._register_devices

    def watched(published: dict[str, dict[str, object]]) -> None:
        held.append(coordinator._lock.locked())
        register(published)

    coordinator._register_devices = watched
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert held == [True]


@pytest.mark.parametrize("action_first", [True, False])
async def test_an_action_and_a_refresh_agree_whichever_order_they_run_in(
    hass: HomeAssistant,
    account: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    action_first: bool,
) -> None:
    """What the entities show must match what the trackers hold.

    A refresh that started before the action must not publish a snapshot
    taken before it, and an action must not publish over a fresher reading
    without the reading's figures in it.
    """
    import asyncio

    coordinator = coordinator_of(account)
    parked = asyncio.Event()
    gate = asyncio.Event()
    collect = coordinator._async_collect

    async def slow_collect() -> object:
        result = await collect()
        parked.set()
        await gate.wait()
        return result

    coordinator._async_collect = slow_collect
    mock_account(aioclient_mock, TWO_TANKS)

    refresh = hass.async_create_task(coordinator.async_refresh())
    await asyncio.wait_for(parked.wait(), timeout=5)

    if not action_first:
        gate.set()
        await asyncio.wait_for(refresh, timeout=5)

    await asyncio.wait_for(
        coordinator.async_set_consumption(1234.0, tank_ids=[FIRST]), timeout=5
    )

    if action_first:
        gate.set()
        await asyncio.wait_for(refresh, timeout=5)

    await hass.async_block_till_done()

    for tank_id in (FIRST, SECOND):
        published = coordinator.data[tank_id]["total_consumption_usable_liters"]
        assert published == tracker_of(coordinator, tank_id).total_litres, tank_id
    assert tracker_of(coordinator, FIRST).total_litres >= 1234.0


async def test_two_tanks_on_one_account_are_written_together_or_not_at_all(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    """Both tanks, one lock, one write.

    Naming two devices used to reach the coordinator as two calls with two
    writes, so a failure on the second left the first permanently changed.
    """
    from unittest.mock import patch

    from homeassistant.util.file import WriteError

    coordinator = coordinator_of(account)
    await coordinator.async_set_consumption(40.0, tank_ids=[FIRST])
    await coordinator.async_set_consumption(90.0, tank_ids=[SECOND])

    devices = []
    for tank_id in (FIRST, SECOND):
        device = tank_device(hass, account, tank_id)
        assert device is not None
        devices.append(device.id)

    with (
        patch.object(Store, "_async_write_data", side_effect=WriteError("full")),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CONSUMPTION,
            {"liters": 1234.0, "device_id": devices},
            blocking=True,
        )

    assert tracker_of(coordinator, FIRST).total_litres == 40.0
    assert tracker_of(coordinator, SECOND).total_litres == 90.0
    assert coordinator.data[FIRST]["total_consumption_usable_liters"] == 40.0
    assert coordinator.data[SECOND]["total_consumption_usable_liters"] == 90.0


async def test_two_device_targets_both_take_effect(
    hass: HomeAssistant, account: MockConfigEntry
) -> None:
    """The other half of the same change: naming two tanks reaches both."""
    coordinator = coordinator_of(account)
    devices = []
    for tank_id in (FIRST, SECOND):
        device = tank_device(hass, account, tank_id)
        assert device is not None
        devices.append(device.id)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CONSUMPTION,
        {"liters": 1234.0, "device_id": devices},
        blocking=True,
    )
    await hass.async_block_till_done()

    for tank_id in (FIRST, SECOND):
        assert tracker_of(coordinator, tank_id).total_litres == 1234.0


async def test_a_reset_shows_immediately_even_with_the_site_down(
    hass: HomeAssistant, account: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A reset is a fact about stored history, not something to go and ask about.

    It persisted the zero and then asked BoilerJuice for fresh readings. With
    the site down the refresh failed, so the entities kept the old total
    while the stored total was zero.
    """
    coordinator = coordinator_of(account)
    await coordinator.async_set_consumption(1234.0, tank_ids=[FIRST])
    await coordinator.async_set_consumption(90.0, tank_ids=[SECOND])
    assert coordinator.data[FIRST]["total_consumption_usable_liters"] == 1234.0

    aioclient_mock.clear_requests()
    aioclient_mock.get(LOGIN_URL, exc=TimeoutError("BoilerJuice is down"))
    aioclient_mock.post(LOGIN_URL, exc=TimeoutError("BoilerJuice is down"))

    device = tank_device(hass, account, FIRST)
    assert device is not None
    await hass.services.async_call(
        DOMAIN, SERVICE_RESET_CONSUMPTION, {"device_id": device.id}, blocking=True
    )
    await hass.async_block_till_done()

    assert tracker_of(coordinator, FIRST).total_litres == 0.0
    assert coordinator.data[FIRST]["total_consumption_usable_liters"] == 0.0
    # The tank nobody named keeps what it had.
    assert coordinator.data[SECOND]["total_consumption_usable_liters"] == 90.0
