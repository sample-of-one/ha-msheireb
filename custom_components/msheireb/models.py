"""Parse the smart-home payload into HVAC zones (roles discovered by label)."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from .const import (
    RAW_TEMP_SCALE,
    ROLE_FAN_AUTO,
    ROLE_FAN_HIGH,
    ROLE_FAN_LOW,
    ROLE_FAN_MEDIUM,
    ROLE_POWER,
    ROLE_ROOM_TEMP,
    ROLE_SETPOINT,
    ROLE_TEMP_DOWN,
    ROLE_TEMP_UP,
)

_ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (ROLE_POWER, re.compile(r"^\s*hvac\s*(ac|power|on\s*/?\s*off)\s*$", re.I)),
    (ROLE_FAN_AUTO, re.compile(r"fan\s*auto", re.I)),
    (ROLE_FAN_LOW, re.compile(r"fan\s*low", re.I)),
    (ROLE_FAN_MEDIUM, re.compile(r"fan\s*med(ium)?", re.I)),
    (ROLE_FAN_HIGH, re.compile(r"fan\s*high", re.I)),
    (ROLE_TEMP_UP, re.compile(r"temp(erature)?\s*up", re.I)),
    (ROLE_TEMP_DOWN, re.compile(r"temp(erature)?\s*down", re.I)),
    (ROLE_SETPOINT, re.compile(r"set\s*-?\s*point", re.I)),
    (ROLE_ROOM_TEMP, re.compile(r"room\s*temp", re.I)),
]


def role_for_label(label: str | None) -> str | None:
    if not label:
        return None
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(label):
            return role
    return None


def _is_on(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().upper()
    if text in ("ON", "TRUE", "1", "OPEN", "ACTIVE"):
        return True
    if text in ("OFF", "FALSE", "0", "CLOSED", "INACTIVE"):
        return False
    return None


def _to_celsius(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
    # API reports C x 100 (e.g. 1950). Tolerate plain Celsius too.
    if abs(value) > 100:
        return round(value / RAW_TEMP_SCALE, 2)
    return float(value)


@dataclass
class Control:
    sn: int
    type_io: str
    type_code: str
    label: str
    value_type: str | None = None
    value: Any = None


@dataclass
class HvacZone:
    """One HVAC device in a room."""

    contract_id: int
    device_id: int
    room_name: str
    device_name: str
    sort_key: tuple[int, int]
    controls: dict[str, Control] = field(default_factory=dict)
    analog: dict[str, Any] = field(default_factory=dict)  # role -> raw value
    digital: dict[str, bool | None] = field(default_factory=dict)  # role -> on/off

    @property
    def key(self) -> str:
        return f"{self.contract_id}_{self.device_id}"

    @property
    def setpoint(self) -> float | None:
        return _to_celsius(self.analog.get(ROLE_SETPOINT))

    @property
    def room_temperature(self) -> float | None:
        return _to_celsius(self.analog.get(ROLE_ROOM_TEMP))

    @property
    def power(self) -> bool | None:
        return self.digital.get(ROLE_POWER)

    def fan_mode(self, roles: tuple[str, ...]) -> str | None:
        for role in roles:
            if self.digital.get(role):
                return role
        return None

    @property
    def is_hvac(self) -> bool:
        return (
            ROLE_TEMP_UP in self.controls
            or ROLE_TEMP_DOWN in self.controls
            or ROLE_POWER in self.controls
            or ROLE_SETPOINT in self.analog
        )


def parse_smart_home(contract_id: int, data: dict[str, Any]) -> dict[str, HvacZone]:
    """Return {zone.key: HvacZone} for all HVAC devices in a smart-home payload."""
    zones: dict[str, HvacZone] = {}
    for room in data.get("rooms") or []:
        room_name = str(room.get("name") or "Room")
        if "preset" in room_name.lower():
            continue  # "Climate Preset"/"Lights Preset" pseudo-rooms
        for device in room.get("devices") or []:
            if device.get("id") is None:
                continue
            zone = HvacZone(
                contract_id=contract_id,
                device_id=int(device["id"]),
                room_name=room_name,
                device_name=str(device.get("name") or "HVAC"),
                sort_key=(int(room.get("sort_order") or 0), int(device.get("sort_order") or 0)),
            )
            for ctl in device.get("controls") or []:
                role = role_for_label(ctl.get("label"))
                if role is None or role == ROLE_ROOM_TEMP or ctl.get("sn") is None:
                    continue
                zone.controls.setdefault(
                    role,
                    Control(
                        sn=int(ctl["sn"]),
                        type_io=str(ctl.get("type_io") or "S"),
                        type_code=str(ctl.get("type_code") or "D"),
                        label=str(ctl.get("label")),
                        value_type=ctl.get("value_type"),
                        value=ctl.get("value"),
                    ),
                )
            status = device.get("status") or {}
            for item in status.get("analog") or []:
                role = role_for_label(item.get("label"))
                if role in (ROLE_SETPOINT, ROLE_ROOM_TEMP):
                    zone.analog[role] = item.get("current_status")
            for item in status.get("digital") or []:
                role = role_for_label(item.get("label"))
                if role:
                    zone.digital[role] = _is_on(item.get("current_status"))
            if zone.is_hvac and "hvac" in (
                zone.device_name.lower() + " " + " ".join(c.label.lower() for c in zone.controls.values())
            ):
                zones[zone.key] = zone
    return zones
