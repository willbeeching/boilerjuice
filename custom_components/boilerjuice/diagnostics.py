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
        # A row count alone hides a gap. These say how far back the history
        # reaches and which months are actually populated, which is what
        # tells a lost season apart from a season that has not started.
        "history_span": _history_span(tracker),
        "history_rows_by_month": _rows_by_month(tracker),
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


def _history_span(tracker: TankTracker) -> dict[str, str] | None:
    """Return the first and last dated rows, or None when there are none."""
    if not tracker.state.history:
        return None
    moments = [moment for moment, _ in tracker.state.history]
    return {
        "first": min(moments).date().isoformat(),
        "last": max(moments).date().isoformat(),
    }


def _rows_by_month(tracker: TankTracker) -> dict[str, int]:
    """Return how many dated rows fall in each month, oldest first.

    A month missing from this map is a month with no recorded consumption.
    Reading it beside "history_span" is how a reset that dropped a heating
    season shows up without anyone opening the stored file.
    """
    counts: dict[str, int] = {}
    for moment, _ in sorted(tracker.state.history, key=lambda row: row[0]):
        key = moment.strftime("%Y-%m")
        counts[key] = counts.get(key, 0) + 1
    return counts


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BoilerJuiceConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one BoilerJuice account.

    An entry that failed to set up has no runtime data, and asking for it
    raised AttributeError, which Home Assistant served as HTTP 500. The
    download was therefore unavailable at the one moment anybody wants
    it: while the integration is stuck in setup_retry. What the config
    entry knows is reported on its own in that case.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return {
            "config_entry_id": entry.entry_id,
            "data": {
                "integration": {
                    "config_entry_version": entry.version,
                    "storage_version": STORAGE_VERSION,
                    "source": entry.source,
                    "tank_is_pinned": bool(entry.data.get(CONF_TANK_ID)),
                    "tanks_are_filtered": bool(entry.options.get(CONF_TANKS)),
                },
                "update_health": {
                    "set_up": False,
                    "state": str(entry.state),
                    # The setup failure's own message. Parse failures carry
                    # the page shape, which is counts and fixed words only.
                    "reason": entry.reason,
                },
                "tanks": [],
            },
        }

    coordinator = runtime.coordinator
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
            "failing_scopes": len(coordinator._failing),
            "worst_parse_failure_run": max(
                coordinator._parse_failures.values(), default=0
            ),
        },
        "tanks": [
            _tank_diagnostics(index, published.get(tank_id, {}), tracker)
            for index, tank_id in enumerate(sorted(coordinator.tank_ids), start=1)
            if (tracker := coordinator.tracker(tank_id)) is not None
        ],
    }
