"""Shared fixtures for the BoilerJuice tests."""

from __future__ import annotations

import pathlib
from typing import Callable

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    """Return the contents of an HTML fixture."""
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def html() -> Callable[[str], str]:
    """Return the fixture loader."""
    return load_fixture


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/boilerjuice in every test."""
    yield
