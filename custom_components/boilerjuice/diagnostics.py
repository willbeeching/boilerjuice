"""Diagnostics for BoilerJuice.

Diagnostics get pasted into public bug reports, so this deliberately carries
no account details: no email, password, tank id, tank name, cookie, CSRF
token or page HTML. What it does carry is enough to debug a scraper: which
fields the page yielded, how the updates are going, and how much history is
stored.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_TANK_ID, CONF_TANKS, DEFAULT_KWH_PER_LITRE
from .runtime import BoilerJuiceConfigEntry
from .storage import STORAGE_VERSION
from .tank import TankTracker

# The scraped fields worth knowing the presence of. Their values are not
# secret, but "was it found at all" is what diagnoses a layout change.
TRACKED_FIELDS = (
    "total_level_percentage",
    "usable_volume_litres",
    "capacity_litres",
    "height_cm",
    "name",
    "model",
    "manufacturer",
    "shape",
    "oil_type",
    "current_price_pence",
)


def _tank_diagnostics(
    index: int, state: dict[str, Any], tracker: TankTracker
) -> dict[str, Any]:
    """Summarise one tank without naming or identifying it."""
    return {
        # Positional, not the real id: the id is account data.
        "tank": f"tank_{index}",
        "parsed_fields": {
            field: state.get(field) is not None for field in TRACKED_FIELDS
        },
        "has_reference_volume": tracker.state.reference_volume is not None,
        "has_reference_level": tracker.state.reference_level is not None,
        "history_rows": len(tracker.state.history),
        "consumption_sample_days": tracker.sample_days,
        "daily_rate_is_manual": tracker.daily_is_manual,
        "has_measured_daily_rate": tracker.state.daily_litres is not None,
        "total_litres": round(tracker.total_litres, 2),
        "last_level_change": (
            None
            if tracker.last_level_change is None
            else tracker.last_level_change.isoformat()
        ),
        "days_until_empty": state.get("days_until_empty"),
        "seasons_with_data": sorted(
            season
            for season in ("winter", "spring", "summer", "autumn")
            if (state.get("seasonal_stats") or {}).get(f"{season}_avg") is not None
        ),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BoilerJuiceConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one BoilerJuice account."""
    coordinator = entry.runtime_data.coordinator
    published = coordinator.data or {}

    return {
        "integration": {
            "config_entry_version": entry.version,
            "storage_version": STORAGE_VERSION,
            "source": entry.source,
            "kwh_per_litre": coordinator.kwh_per_litre,
            "kwh_per_litre_is_default": (
                coordinator.kwh_per_litre == DEFAULT_KWH_PER_LITRE
            ),
            "tank_is_pinned": bool(entry.data.get(CONF_TANK_ID)),
            "tanks_are_filtered": bool(entry.options.get(CONF_TANKS)),
        },
        "update_health": {
            "last_update_success": coordinator.last_update_success,
            "last_exception_type": (
                type(coordinator.last_exception).__name__
                if coordinator.last_exception
                else None
            ),
            "tank_count": len(coordinator.tank_ids),
            "consecutive_parse_failures": coordinator._consecutive_parse_failures,
        },
        "tanks": [
            _tank_diagnostics(index, published.get(tank_id, {}), tracker)
            for index, tank_id in enumerate(sorted(coordinator.tank_ids), start=1)
            if (tracker := coordinator.tracker(tank_id)) is not None
        ],
    }
