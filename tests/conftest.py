"""Shared fixtures for the BoilerJuice tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from .helpers import load_fixture

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def html() -> Callable[[str], str]:
    """Return the HTML fixture loader."""
    return load_fixture


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/boilerjuice in every test."""
    yield
