"""Reset and set_consumption rewrite stored history, so targeting must be exact.

The resolver used to advertise entity, area and label targets but only read
device_id and entry_id. Anything else fell through to "every configured
account", which silently wiped the other tank's history.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
import voluptuous as vol
from custom_components.boilerjuice import (
    SERVICE_RESET_CONSUMPTION,
    SERVICE_SET_CONSUMPTION,
    async_setup_services,
)
from custom_components.boilerjuice.const import DOMAIN
from custom_components.boilerjuice.coordinator import BoilerJuiceDataUpdateCoordinator
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import (
    TANK_ID,
    coordinator_of,
    mock_site,
    reading_of,
    setup_account,
    tank_device,
    tank_page,
    tracker_of,
)


@pytest.fixture
async def two_accounts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> tuple[MockConfigEntry, MockConfigEntry]:
    """Two configured accounts, each with recorded consumption."""
    first = await setup_account(
        hass, aioclient_mock, email="one@example.com", tank_id="111111", litres=2000
    )
    second = await setup_account(
        hass, aioclient_mock, email="two@example.com", tank_id="222222", litres=1500
    )

    for entry, tank_id, total in ((first, "111111", 40.0), (second, "222222", 90.0)):
        tracker_of(coordinator_of(entry), tank_id).state.total_litres = total

    return first, second


def totals(hass: HomeAssistant, *entries: MockConfigEntry) -> list[float]:
    """Return each account's recorded total consumption."""
    return [
        sum(
            coordinator_of(entry).tracker(tank_id).total_litres
            for tank_id in coordinator_of(entry).tank_ids
        )
        for entry in entries
    ]


async def test_an_unresolvable_target_is_refused_not_broadcast(
    hass: HomeAssistant, two_accounts
) -> None:
    """An area holding no BoilerJuice tank must not mean "reset everything"."""
    first, second = two_accounts

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"area_id": "kitchen"},
            blocking=True,
        )

    assert totals(hass, first, second) == [40.0, 90.0]


async def test_no_target_with_several_accounts_is_refused(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_RESET_CONSUMPTION, {}, blocking=True
        )

    assert totals(hass, first, second) == [40.0, 90.0]


async def test_a_device_target_reaches_only_that_account(
    hass: HomeAssistant, two_accounts
) -> None:
    _, second = two_accounts
    device = tank_device(hass, two_accounts[0], "111111")
    assert device is not None

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"device_id": device.id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, second) == [90.0]


async def test_an_entity_target_reaches_only_that_account(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts
    entity_registry = er.async_get(hass)
    entity_ids = [
        entity.entity_id
        for entity in er.async_entries_for_config_entry(entity_registry, first.entry_id)
    ]
    assert entity_ids

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"entity_id": entity_ids[0]},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, second) == [90.0]


async def test_an_unknown_entity_target_is_refused(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"entity_id": "sensor.does_not_exist"},
            blocking=True,
        )

    assert totals(hass, first, second) == [40.0, 90.0]


async def test_set_consumption_uses_the_configured_energy_content(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock, litres=2000)
    coordinator = coordinator_of(entry)
    coordinator._kwh_per_litre = 9.6

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CONSUMPTION,
        {"liters": 100.0, "entry_id": entry.entry_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert tracker_of(coordinator).total_kwh(
        coordinator.kwh_per_litre
    ) == pytest.approx(960.0)
    assert reading_of(coordinator)["total_consumption_usable_kwh"] == pytest.approx(
        960.0
    )


async def test_a_label_target_reaches_only_that_account(
    hass: HomeAssistant, two_accounts
) -> None:
    from homeassistant.helpers import label_registry as lr

    first, second = two_accounts
    label = lr.async_get(hass).async_create("Oil")
    device = tank_device(hass, first, "111111")
    dr.async_get(hass).async_update_device(device.id, labels={label.label_id})

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"label_id": label.label_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, first, second) == [0.0, 90.0]


async def test_an_area_target_reaches_only_that_account(
    hass: HomeAssistant, two_accounts
) -> None:
    from homeassistant.helpers import area_registry as ar

    first, second = two_accounts
    area = ar.async_get(hass).async_create("Utility")
    device = tank_device(hass, second, "222222")
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"area_id": area.id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, first, second) == [40.0, 0.0]


async def test_a_target_spanning_two_accounts_is_refused(
    hass: HomeAssistant, two_accounts
) -> None:
    """Half an account rewritten is worse than none.

    The accounts were written one after another, so a failure on the second
    left the first already changed and saved, with the call reported as
    failed. There is no undo for a document that has already been written,
    so the action refuses the whole thing instead.
    """
    first, second = two_accounts

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"entry_id": [first.entry_id, second.entry_id]},
            blocking=True,
        )

    assert raised.value.translation_key == "one_account_at_a_time"
    assert totals(hass, first, second) == [40.0, 90.0]


async def test_an_area_holding_both_accounts_is_refused_too(
    hass: HomeAssistant, two_accounts
) -> None:
    """The same rule, reached through a target rather than a list."""
    from homeassistant.helpers import area_registry as ar

    first, second = two_accounts
    area = ar.async_get(hass).async_create("Plant room")
    for entry, tank_id in ((first, "111111"), (second, "222222")):
        device = tank_device(hass, entry, tank_id)
        assert device is not None
        dr.async_get(hass).async_update_device(device.id, area_id=area.id)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_RESET_CONSUMPTION, {"area_id": area.id}, blocking=True
        )

    assert totals(hass, first, second) == [40.0, 90.0]


async def test_one_entry_id_in_a_list_is_still_accepted(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"entry_id": [first.entry_id]},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, first, second) == [0.0, 90.0]


async def test_an_unknown_entry_id_is_refused(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"entry_id": "not-a-real-entry"},
            blocking=True,
        )

    assert totals(hass, first, second) == [40.0, 90.0]


async def test_an_unknown_device_id_is_refused(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"device_id": "not-a-real-device"},
            blocking=True,
        )

    assert totals(hass, first, second) == [40.0, 90.0]


async def test_a_single_account_needs_no_target(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)
    tracker_of(coordinator_of(entry)).state.total_litres = 40.0

    await hass.services.async_call(DOMAIN, SERVICE_RESET_CONSUMPTION, {}, blocking=True)
    await hass.async_block_till_done()

    assert totals(hass, entry) == [0.0]


async def test_calling_a_service_with_nothing_loaded_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)
    # Keep the services registered while the entry is unloaded, which is what
    # a call racing an unload would see.
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    async_setup_services(hass)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_RESET_CONSUMPTION, {}, blocking=True
        )


async def test_set_consumption_also_sets_the_daily_rate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CONSUMPTION,
        {"liters": 100.0, "daily": 7.5, "entry_id": entry.entry_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert tracker_of(coordinator).daily_litres == 7.5
    assert reading_of(coordinator)["daily_consumption_usable_liters"] == 7.5


async def test_set_consumption_refuses_an_account_with_no_reading_yet(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Doing nothing quietly left the user believing their totals were set."""
    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)
    coordinator.data = None

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CONSUMPTION,
            {"liters": 100.0, "entry_id": entry.entry_id},
            blocking=True,
        )

    assert tracker_of(coordinator).total_litres == 0.0


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(10_000_001, id="above-the-storage-bound"),
        pytest.param(float("inf"), id="infinite"),
        pytest.param(float("nan"), id="not-a-number"),
        pytest.param(-1, id="negative"),
        pytest.param("plenty", id="not-numeric"),
    ],
)
async def test_the_action_refuses_a_total_storage_would_reject(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, bad: object
) -> None:
    """A figure accepted here and refused on load costs the whole history.

    cv.positive_float took 10,000,001 litres and reported success. The next
    start could not read the document back and discarded the account.
    """
    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)
    await coordinator.async_set_consumption(40.0)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CONSUMPTION,
            {"liters": bad, "entry_id": entry.entry_id},
            blocking=True,
        )

    assert tracker_of(coordinator).total_litres == 40.0

    # The point of the bound: what survived is still readable.
    fresh = BoilerJuiceDataUpdateCoordinator(hass, entry)
    try:
        state, reason = await fresh._store.async_load()
        assert reason is None
        assert state.tanks[TANK_ID].total_litres == 40.0
    finally:
        await fresh.async_close()


async def test_the_daily_rate_is_bounded_the_same_way(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_account(hass, aioclient_mock)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CONSUMPTION,
            {"liters": 40.0, "daily": float("inf"), "entry_id": entry.entry_id},
            blocking=True,
        )


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_RESET_CONSUMPTION, {}),
        (SERVICE_SET_CONSUMPTION, {"liters": 1234.0}),
    ],
)
async def test_a_failed_save_leaves_the_tracker_as_it_was(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    service: str,
    data: dict[str, object],
) -> None:
    """Reporting nothing recorded must mean nothing changed in memory either.

    The mutation happened before the write, so a failed write left the new
    totals live and a later poll persisted them anyway.
    """
    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)
    await coordinator.async_set_consumption(40.0)
    before = reading_of(coordinator)["total_consumption_usable_liters"]
    assert before == 40.0

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            side_effect=OSError("no space left on device"),
        ),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            DOMAIN, service, {**data, "entry_id": entry.entry_id}, blocking=True
        )

    assert tracker_of(coordinator).total_litres == 40.0
    assert reading_of(coordinator)["total_consumption_usable_liters"] == 40.0

    # The next poll must not quietly persist the change that was refused.
    # It burns a real 250 L (2000 down to 1750), so the only correct total
    # is 290: a leaked reset would give 250, a leaked set would give 1484.
    mock_site(aioclient_mock, tank_html=tank_page(percentage=70, litres=1750))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    fresh = BoilerJuiceDataUpdateCoordinator(hass, entry)
    try:
        state, _ = await fresh._store.async_load()
        assert state.tanks[TANK_ID].total_litres == 290.0
    finally:
        await fresh.async_close()


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_RESET_CONSUMPTION, {}),
        (SERVICE_SET_CONSUMPTION, {"liters": 100.0}),
    ],
)
@pytest.mark.parametrize(
    "typo", ["deviceid", "device", "entityid", "entry", "areaid", "target"]
)
async def test_a_misspelled_target_is_refused_not_widened(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    service: str,
    data: dict[str, object],
    typo: str,
) -> None:
    """A field nobody reads is not a target, and must not read as none.

    ALLOW_EXTRA accepted `deviceid`, left no recognised target, and the
    single-account fallback then rewrote every tank on the account: the
    opposite of what the caller asked for, from one missing underscore.
    """
    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)
    await coordinator.async_set_consumption(40.0)

    device = tank_device(hass, entry, TANK_ID)
    assert device is not None

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, service, {**data, typo: device.id}, blocking=True
        )

    assert tracker_of(coordinator).total_litres == 40.0


async def test_a_label_on_an_area_reaches_the_tanks_in_it(
    hass: HomeAssistant, two_accounts
) -> None:
    """A label can be put on an area, not only on a device or an entity.

    That spelling was unresolved, so it looked like no target at all, and on
    a single-account system that means every tank.
    """
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import label_registry as lr

    first, second = two_accounts
    label = lr.async_get(hass).async_create("Plant room")
    area = ar.async_get(hass).async_create("Utility")
    ar.async_get(hass).async_update(area.id, labels={label.label_id})
    device = tank_device(hass, second, "222222")
    assert device is not None
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"label_id": label.label_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, first, second) == [40.0, 0.0]


async def test_a_label_on_an_empty_area_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An area label that reaches no tank must not widen to the account."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import label_registry as lr

    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)
    await coordinator.async_set_consumption(40.0)

    label = lr.async_get(hass).async_create("Loft")
    area = ar.async_get(hass).async_create("Loft")
    ar.async_get(hass).async_update(area.id, labels={label.label_id})

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"label_id": label.label_id},
            blocking=True,
        )

    assert tracker_of(coordinator).total_litres == 40.0


async def test_a_floor_target_reaches_only_the_tanks_on_it(
    hass: HomeAssistant, two_accounts
) -> None:
    """A floor is a set of areas, and used to resolve to nothing at all.

    Unresolved it looked like no target, which on a single account means
    every tank: picking one floor in the UI erased the lot. Two accounts
    here, so the fallback cannot pass this by accident.
    """
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import floor_registry as fr

    first, second = two_accounts
    floor = fr.async_get(hass).async_create("Ground")
    area = ar.async_get(hass).async_create("Utility", floor_id=floor.floor_id)
    device = tank_device(hass, second, "222222")
    assert device is not None
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)

    await hass.services.async_call(
        DOMAIN, SERVICE_RESET_CONSUMPTION, {"floor_id": floor.floor_id}, blocking=True
    )
    await hass.async_block_till_done()

    assert totals(hass, first, second) == [40.0, 0.0]


async def test_a_floor_with_no_boilerjuice_tank_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    from homeassistant.helpers import floor_registry as fr

    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)
    await coordinator.async_set_consumption(40.0)

    floor = fr.async_get(hass).async_create("Loft")

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"floor_id": floor.floor_id},
            blocking=True,
        )

    assert tracker_of(coordinator).total_litres == 40.0


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_RESET_CONSUMPTION, {}),
        (SERVICE_SET_CONSUMPTION, {"liters": 1234.0}),
    ],
)
async def test_a_write_home_assistant_swallows_is_still_a_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    service: str,
    data: dict[str, object],
) -> None:
    """Store logs a failed write and returns normally; we must not.

    This is what a full disk actually looks like from inside an
    integration: async_save raises nothing at all. Mocking async_save to
    raise tested a failure mode Home Assistant does not have.
    """
    from homeassistant.helpers.storage import Store
    from homeassistant.util.file import WriteError

    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)
    await coordinator.async_set_consumption(40.0)

    with (
        patch.object(
            Store, "_async_write_data", side_effect=WriteError("no space left")
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            DOMAIN, service, {**data, "entry_id": entry.entry_id}, blocking=True
        )

    assert raised.value.translation_key == "save_failed"
    assert tracker_of(coordinator).total_litres == 40.0
    assert reading_of(coordinator)["total_consumption_usable_liters"] == 40.0


async def test_a_foreign_but_real_target_is_a_validation_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Naming somebody else's device is a mistake in the call, not a fault.

    ServiceValidationError is what Home Assistant shows as the caller's
    error; a plain HomeAssistantError reads as the integration failing.
    """
    entry = await setup_account(hass, aioclient_mock)

    other = MockConfigEntry(domain="demo")
    other.add_to_hass(hass)
    foreign = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("demo", "somebody-elses-thing")},
        name="Somebody else's thing",
    )

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"device_id": foreign.id},
            blocking=True,
        )

    assert raised.value.translation_key == "no_boilerjuice_target"
    assert tracker_of(coordinator_of(entry)).total_litres == 0.0


async def test_a_boilerjuice_device_with_no_tank_is_a_validation_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A device on our entry that names no tank must not widen to the account."""
    entry = await setup_account(hass, aioclient_mock)
    coordinator_of(entry).tracker(TANK_ID).state.total_litres = 40.0

    stray = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "account-summary")},
        name="Account",
    )

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"device_id": stray.id},
            blocking=True,
        )

    assert raised.value.translation_key == "no_boilerjuice_target"
    assert tracker_of(coordinator_of(entry)).total_litres == 40.0


async def test_a_deviceless_entity_on_our_account_is_a_validation_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An entity with no device names no tank, and must not widen."""
    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)
    await coordinator.async_set_consumption(40.0)

    registry = er.async_get(hass)
    orphan = registry.async_get_or_create(
        "sensor", DOMAIN, "no-device-here", config_entry=entry
    )
    assert orphan.device_id is None

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"entity_id": orphan.entity_id},
            blocking=True,
        )

    assert raised.value.translation_key == "no_boilerjuice_target"
    assert tracker_of(coordinator).total_litres == 40.0


async def test_a_translated_storage_error_keeps_its_own_words(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Only an untranslated failure becomes "could not be saved"."""
    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)

    already = HomeAssistantError(
        translation_domain=DOMAIN, translation_key="no_reading_yet"
    )
    with (
        patch.object(type(coordinator), "async_reset_consumption", side_effect=already),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            {"entry_id": entry.entry_id},
            blocking=True,
        )

    assert raised.value is already


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_RESET_CONSUMPTION, {}),
        (SERVICE_SET_CONSUMPTION, {"liters": 100.0}),
    ],
)
async def test_a_failed_save_is_reported_as_a_sentence_not_a_traceback(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    service: str,
    data: dict[str, object],
) -> None:
    """A full disk raised OSError at the user, filename and all."""
    entry = await setup_account(hass, aioclient_mock)

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            side_effect=OSError("No space left on device: '/config/.storage/x'"),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            DOMAIN, service, {**data, "entry_id": entry.entry_id}, blocking=True
        )

    assert raised.value.translation_key == "save_failed"


async def test_a_translated_failure_is_not_reworded_as_a_save_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The caller's own mistake is already phrased for them."""
    entry = await setup_account(hass, aioclient_mock)
    coordinator_of(entry).data = None

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CONSUMPTION,
            {"liters": 100.0, "entry_id": entry.entry_id},
            blocking=True,
        )

    assert raised.value.translation_key == "no_reading_yet"


def test_the_action_definitions_and_translations_agree() -> None:
    """A field with no translation shows up untranslated in the UI."""
    import json
    import pathlib

    import yaml

    root = pathlib.Path("custom_components/boilerjuice")
    actions = yaml.safe_load((root / "services.yaml").read_text(encoding="utf-8"))

    for name in ("strings.json", "translations/en.json"):
        translated = json.loads((root / name).read_text(encoding="utf-8"))["services"]
        assert set(translated) == set(actions), name
        for action, definition in actions.items():
            assert set(translated[action]["fields"]) == set(
                definition.get("fields", {})
            ), f"{name}: {action}"


def test_every_translation_key_raised_in_the_code_exists() -> None:
    """A key with no entry reaches the user as the key itself."""
    import json
    import pathlib
    import re

    root = pathlib.Path("custom_components/boilerjuice")
    used = {
        match.group(1)
        for source in root.glob("*.py")
        for match in re.finditer(
            r'translation_key="([^"]+)"', source.read_text(encoding="utf-8")
        )
    }
    assert used, "no translation keys found; the pattern has gone stale"

    for name in ("strings.json", "translations/en.json"):
        document = json.loads((root / name).read_text(encoding="utf-8"))
        raised = set(document["exceptions"]) | set(document["issues"])
        defined = raised | set(document["entity"]["sensor"])
        assert used <= defined, f"{name}: undefined {sorted(used - defined)}"
        assert raised <= used, f"{name}: unused {sorted(raised - used)}"


def test_both_actions_accept_a_boilerjuice_target() -> None:
    """Without a target block the UI offers no way to pick a tank.

    Filtered by entity and nothing else. A service target does not take a
    device filter: Home Assistant's own runtime schema still accepts one, so
    only hassfest catches it, and it caught this on the first hosted run.
    Every one of the 489 target blocks Home Assistant ships filters by entity
    alone. The picker still offers the devices and areas that hold these
    entities, which is what makes per-tank targeting work.
    """
    import pathlib

    import yaml

    actions = yaml.safe_load(
        pathlib.Path("custom_components/boilerjuice/services.yaml").read_text(
            encoding="utf-8"
        )
    )

    for name, definition in actions.items():
        target = definition["target"]
        assert target["entity"]["integration"] == DOMAIN, name
        assert "device" not in target, name
        assert set(target) <= {"entity", "primary_entities_only"}, name


# --- a reset keeps the seasonal history -----------------------------------


async def _tank_with_history(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    """Return an account whose tank has a year of dated consumption."""
    entry = await setup_account(hass, aioclient_mock)
    coordinator = coordinator_of(entry)
    tracker = tracker_of(coordinator)

    winter = dt_util.now().replace(month=1, day=15)
    tracker.state.history = [
        (winter + timedelta(days=offset), 9.0) for offset in range(10)
    ]
    tracker.state.total_litres = 500.0
    return entry, coordinator, tracker


def _by_day(tracker) -> dict[str, float]:
    """Return the tank's history as one rounded total per calendar day."""
    totals: dict[str, float] = {}
    for moment, litres in tracker.state.history:
        key = moment.date().isoformat()
        totals[key] = round(totals.get(key, 0.0) + litres, 3)
    return totals


async def test_a_reset_keeps_the_consumption_history(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Zeroing the counter used to cost a whole heating season.

    The running total answers "how much since I last zeroed it". The dated
    history answers "what does this tank burn in January". One reset in
    April took a year of seasonal averages with it.
    """
    _, _, tracker = await _tank_with_history(hass, aioclient_mock)
    before = _by_day(tracker)

    await hass.services.async_call(DOMAIN, SERVICE_RESET_CONSUMPTION, {}, blocking=True)
    await hass.async_block_till_done()

    assert tracker.state.total_litres == 0.0
    # Compared per day, not row for row: the next publish collapses the
    # history to one entry per day, which is what it has always done.
    assert _by_day(tracker) == before


async def test_a_reset_clears_the_history_when_asked(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """History that is itself wrong still needs a way out."""
    _, _, tracker = await _tank_with_history(hass, aioclient_mock)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"clear_history": True},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert tracker.state.total_litres == 0.0
    assert tracker.state.history == []


async def test_the_kept_history_survives_a_restart(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Keeping it in memory is no use if the reset wrote an empty document."""
    _, coordinator, tracker = await _tank_with_history(hass, aioclient_mock)
    kept = len(tracker.state.history)

    await hass.services.async_call(DOMAIN, SERVICE_RESET_CONSUMPTION, {}, blocking=True)
    await hass.async_block_till_done()

    account, problem = await coordinator._store.async_load()
    assert problem is None
    assert len(account.tanks[TANK_ID].history) == kept
