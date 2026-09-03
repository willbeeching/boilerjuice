"""The device-registry compatibility layer, both branches.

Home Assistant 2026.9 made device identifiers unique per config entry. The
old lookup raises in tests on current Home Assistant; the new one does not
exist on the supported floor. Neither branch can be exercised by the other
lane, so both are tested against stand-in registries here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from custom_components.boilerjuice.const import DOMAIN
from custom_components.boilerjuice.helpers import (
    async_tank_device,
    device_config_entry_ids,
    device_tank_ids,
    normalise_email,
)


@dataclass
class FakeDevice:
    """Stands in for a DeviceEntry."""

    id: str = "device-1"
    identifiers: set = field(default_factory=lambda: {(DOMAIN, "123456")})


class ModernRegistry:
    """A 2026.9 registry: identifiers are unique per config entry."""

    def __init__(self, device: FakeDevice | None) -> None:
        """Record what it is asked for."""
        self._device = device
        self.calls: list[tuple] = []

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        """Look a device up within one config entry."""
        self.calls.append((identifier, config_entry_id))
        return self._device

    def async_get_device(self, identifiers=None, connections=None):
        """Fail: this is the call 2026.9 deprecated."""
        raise AssertionError("the deprecated lookup must not be used here")


class LegacyRegistry:
    """A 2025.2 registry: only the global lookup exists."""

    def __init__(self, device: FakeDevice | None) -> None:
        """Record what it is asked for."""
        self._device = device
        self.calls: list[dict] = []

    def async_get_device(self, identifiers=None, connections=None):
        """Look a device up globally, as versions before 2026.9 did."""
        self.calls.append({"identifiers": identifiers})
        return self._device


@pytest.mark.parametrize("found", [True, False])
def test_the_modern_registry_is_asked_per_config_entry(hass, found: bool) -> None:
    device = FakeDevice() if found else None
    registry = ModernRegistry(device)

    with patch(
        "custom_components.boilerjuice.helpers.dr.async_get", return_value=registry
    ):
        assert async_tank_device(hass, "123456", "entry-1") is device

    assert registry.calls == [((DOMAIN, "123456"), "entry-1")]


@pytest.mark.parametrize("found", [True, False])
def test_the_legacy_registry_falls_back_to_the_global_lookup(hass, found: bool) -> None:
    device = FakeDevice() if found else None
    registry = LegacyRegistry(device)

    with patch(
        "custom_components.boilerjuice.helpers.dr.async_get", return_value=registry
    ):
        assert async_tank_device(hass, "123456", "entry-1") is device

    assert registry.calls == [{"identifiers": {(DOMAIN, "123456")}}]


def test_a_single_config_entry_device_reports_just_that_entry() -> None:
    @dataclass
    class Modern:
        config_entry_id: str = "entry-1"
        config_entries: set = field(default_factory=lambda: {"entry-1", "stale"})

    # The singular field wins: config_entries is a deprecated compatibility
    # property that can report merged entries for a restored composite.
    assert device_config_entry_ids(Modern()) == {"entry-1"}


def test_a_legacy_device_reports_its_config_entry_set() -> None:
    @dataclass
    class Legacy:
        config_entries: set = field(default_factory=lambda: {"entry-1", "entry-2"})

    assert device_config_entry_ids(Legacy()) == {"entry-1", "entry-2"}


def test_tank_ids_ignore_other_integrations_identifiers() -> None:
    device = FakeDevice(identifiers={(DOMAIN, "123456"), ("other", "999")})

    assert device_tank_ids(device) == {"123456"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Me@Example.com", "me@example.com"), ("  a@b.c  ", "a@b.c")],
)
def test_normalise_email(raw: str, expected: str) -> None:
    assert normalise_email(raw) == expected
