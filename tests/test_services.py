"""Reset and set_consumption rewrite stored history, so targeting must be exact.

The resolver used to advertise entity, area and label targets but only read
device_id and entry_id. Anything else fell through to "every configured
account", which silently wiped the other tank's history.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from custom_components.boilerjuice import (
    SERVICE_RESET_CONSUMPTION,
    SERVICE_SET_CONSUMPTION,
    async_setup_services,
)
from custom_components.boilerjuice.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .helpers import (
    TANK_ID,
    coordinator_of,
    reading_of,
    setup_account,
    tank_device,
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


async def test_a_list_of_entry_ids_is_accepted(
    hass: HomeAssistant, two_accounts
) -> None:
    first, second = two_accounts

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_CONSUMPTION,
        {"entry_id": [first.entry_id, second.entry_id]},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert totals(hass, first, second) == [0.0, 0.0]


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
    """Without a target block the UI offers no way to pick a tank."""
    import pathlib

    import yaml

    actions = yaml.safe_load(
        pathlib.Path("custom_components/boilerjuice/services.yaml").read_text(
            encoding="utf-8"
        )
    )

    for name, definition in actions.items():
        assert definition["target"]["device"]["integration"] == DOMAIN, name
        assert definition["target"]["entity"]["integration"] == DOMAIN, name
