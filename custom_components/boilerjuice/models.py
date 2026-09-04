"""Typed, immutable models for what we read from BoilerJuice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TankReading:
    """One validated reading of one tank.

    Every optional field is either a value that passed validation or None.
    There is deliberately no zero default: "we could not read the volume" and
    "the tank is empty" must never be the same value.
    """

    tank_id: str
    level_percentage: float | None = None
    volume_litres: int | None = None
    capacity_litres: int | None = None
    height_cm: int | None = None
    name: str | None = None
    model: str | None = None
    model_id: str | None = None
    manufacturer: str | None = None
    shape: str | None = None
    oil_type: str | None = None

    @property
    def has_measurement(self) -> bool:
        """Whether this reading can drive the consumption engine at all."""
        return self.level_percentage is not None or self.volume_litres is not None

    def as_state(self) -> dict[str, Any]:
        """Return the dict the coordinator publishes to the sensors.

        Absent fields stay absent so a sensor shows "unknown" rather than a
        made-up zero.
        """
        state: dict[str, Any] = {"id": self.tank_id}

        if self.level_percentage is not None:
            # BoilerJuice publishes a single level ("total oil remaining")
            # where it used to expose separate total and usable figures.
            state["total_level_percentage"] = self.level_percentage
            state["usable_level_percentage"] = self.level_percentage
        if self.volume_litres is not None:
            state["current_volume_litres"] = self.volume_litres
            state["usable_volume_litres"] = self.volume_litres

        for key, value in (
            ("capacity_litres", self.capacity_litres),
            ("height_cm", self.height_cm),
            ("name", self.name),
            ("model", self.model),
            ("model_id", self.model_id),
            ("manufacturer", self.manufacturer),
            ("shape", self.shape),
            ("oil_type", self.oil_type),
        ):
            if value is not None:
                state[key] = value

        return state
