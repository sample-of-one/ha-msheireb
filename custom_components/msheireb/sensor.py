"""Diagnostic sensors: auth, polling health, token, command results/counters."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .api import AUTH_FAILED, AUTH_OK, AUTH_REFRESHING, AUTH_RELOGIN
from .const import CMD_RESULTS, DOMAIN, INTEGRATION_VERSION, UNLOCK_DURATION
from .coordinator import MsheirebCoordinator
from .door import DOOR_FAILED, DOOR_LOCKED, DOOR_OPEN, DOOR_STATES, DOOR_UNLOCKING
from .entity import room_device, room_entity_id, zone_needs_disambiguation


DOOR_ICONS = {DOOR_LOCKED: "mdi:lock", DOOR_UNLOCKING: "mdi:lock-clock", DOOR_OPEN: "mdi:door-open",
              DOOR_FAILED: "mdi:lock-alert"}


def account_device(coordinator: MsheirebCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"account_{coordinator.config_entry.entry_id}")},
        name="Msheireb portal",
        manufacturer="Msheireb Properties",
        model="Resident portal API",
        sw_version=INTEGRATION_VERSION,
    )


class HealthEntity(Entity):
    """Entity driven by coordinator health callbacks; always available."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MsheirebCoordinator, key: str) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = account_device(coordinator)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_health_listener(self.async_write_ha_state))


@dataclass(frozen=True)
class HealthSensorDef:
    key: str
    value: Callable[[MsheirebCoordinator], Any]
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    unit: str | None = None
    options: list[str] | None = None
    attrs: Callable[[MsheirebCoordinator], dict[str, Any]] | None = None


SENSORS: tuple[HealthSensorDef, ...] = (
    HealthSensorDef(
        "auth_status",
        lambda c: c.health.auth_status,
        device_class=SensorDeviceClass.ENUM,
        options=[AUTH_OK, AUTH_REFRESHING, AUTH_RELOGIN, AUTH_FAILED],
    ),
    HealthSensorDef("last_update", lambda c: c.health.last_success, device_class=SensorDeviceClass.TIMESTAMP),
    HealthSensorDef(
        "last_error",
        lambda c: c.health.last_error,
        attrs=lambda c: {
            "type": c.health.last_error_type,
            "at": c.health.last_error_at.isoformat() if c.health.last_error_at else None,
        },
    ),
    HealthSensorDef(
        "response_time",
        lambda c: c.last_response_ms,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        unit=UnitOfTime.MILLISECONDS,
    ),
    HealthSensorDef(
        "consecutive_failures", lambda c: c.health.consecutive_failures, state_class=SensorStateClass.MEASUREMENT
    ),
    HealthSensorDef("token_expiry", lambda c: c.token_expires, device_class=SensorDeviceClass.TIMESTAMP),
    HealthSensorDef(
        "commands_sent", lambda c: c.health.commands_sent, state_class=SensorStateClass.TOTAL_INCREASING
    ),
    HealthSensorDef(
        "commands_confirmed", lambda c: c.health.commands_confirmed, state_class=SensorStateClass.TOTAL_INCREASING
    ),
    HealthSensorDef(
        "command_retries", lambda c: c.health.command_retries, state_class=SensorStateClass.TOTAL_INCREASING
    ),
    HealthSensorDef(
        "drift_events",
        lambda c: c.drift.events,
        state_class=SensorStateClass.TOTAL_INCREASING,
        attrs=lambda c: (
            {"last_room": c.drift.last_event.room, "last_changes": c.drift.last_event.changes,
             "last_action": c.drift.last_event.action, "last_at": c.drift.last_event.at}
            if c.drift.last_event else {}
        ),
    ),
    HealthSensorDef(
        "commands_failed", lambda c: c.health.commands_failed, state_class=SensorStateClass.TOTAL_INCREASING
    ),
)


class HealthSensor(HealthEntity, SensorEntity):
    def __init__(self, coordinator: MsheirebCoordinator, d: HealthSensorDef) -> None:
        super().__init__(coordinator, d.key)
        self._def = d
        self._attr_device_class = d.device_class
        self._attr_state_class = d.state_class
        self._attr_native_unit_of_measurement = d.unit
        if d.options:
            self._attr_options = d.options

    @property
    def native_value(self) -> Any:
        return self._def.value(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self._def.attrs(self.coordinator) if self._def.attrs else None


class LastCommandSensor(HealthEntity, SensorEntity):
    """Result of the last command sent to a room."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CMD_RESULTS

    def __init__(self, coordinator: MsheirebCoordinator, zone, disambiguate: bool = False) -> None:
        super().__init__(coordinator, f"last_command_{zone.key}")
        self._attr_translation_key = "last_command"
        self._zone_key = zone.key
        self._attr_device_info = room_device(zone, disambiguate)

    @property
    def native_value(self) -> str | None:
        rec = self.coordinator.health.commands.get(self._zone_key)
        return rec.result if rec else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = self.coordinator.health.commands.get(self._zone_key)
        return rec.as_attributes() if rec else None


class DoorSensor(SensorEntity):
    """Door: locked / unlocking / open / failed, driven by the unlock button (portal-like feedback).

    The state comes from the door state machine only, so coordinator polls never overwrite a
    transient state; every transition is written immediately.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = DOOR_STATES
    _attr_translation_key = "door"

    def __init__(self, coordinator: MsheirebCoordinator, contract_id: int, title: str) -> None:
        from .entity import apartment_device

        self.coordinator = coordinator
        self._contract_id = contract_id
        self._attr_unique_id = f"{contract_id}_door"
        self._attr_device_info = apartment_device(contract_id, title)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.door.async_add_listener(self.async_write_ha_state))
        self.async_on_remove(self.coordinator.async_add_listener(self._lock_status_updated))

    @callback
    def _lock_status_updated(self) -> None:
        self.async_write_ha_state()  # attributes (lock status) only; the state is the door's

    @property
    def native_value(self) -> str:
        return self.coordinator.door.get(self._contract_id).state

    @property
    def icon(self) -> str:
        return DOOR_ICONS.get(self.native_value, "mdi:lock")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cd = (self.coordinator.data or {}).get(self._contract_id)
        locks = ((cd.lock or {}).get("locks") or []) if cd else []
        first = locks[0] if locks else {}
        status = first.get("current_lock_status") or {}
        last = self.coordinator.last_unlock.get(self._contract_id) or {}
        return {
            "last_result": last.get("result"),
            "last_unlock_at": last.get("at"),
            "unlock_duration": last.get("duration", UNLOCK_DURATION),
            "message": last.get("message"),
            "problem": self.coordinator.door.get(self._contract_id).problem,
            "lock_connected": status.get("connected"),
            "lock_outdated": status.get("outdated"),
            "low_battery": first.get("low_battery"),
        }


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MsheirebCoordinator = entry.runtime_data
    async_add_entities(HealthSensor(coordinator, d) for d in SENSORS)
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = []
        for cid, cd in (coordinator.data or {}).items():
            if (cd.lock or {}).get("locks") and f"door_{cid}" not in known:
                known.add(f"door_{cid}")
                ent = DoorSensor(coordinator, cid, cd.title)
                ent.entity_id = "sensor." + slugify(f"msheireb {cd.title} door")
                new.append(ent)
            for zone in sorted(cd.zones.values(), key=lambda z: z.sort_key):
                if zone.key not in known:
                    known.add(zone.key)
                    ent = LastCommandSensor(coordinator, zone, zone_needs_disambiguation(cd, zone))
                    ent.entity_id = room_entity_id("sensor", cd, zone, "last command")
                    new.append(ent)
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))
