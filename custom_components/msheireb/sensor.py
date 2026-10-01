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
from .const import CMD_RESULTS, DOMAIN, INTEGRATION_VERSION
from .coordinator import MsheirebCoordinator
from .entity import room_device, room_entity_id, zone_needs_disambiguation


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


class LastUnlockSensor(HealthEntity, SensorEntity):
    """Result of the last door unlock sent from Home Assistant."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["success", "failed"]

    def __init__(self, coordinator: MsheirebCoordinator, contract_id: int, title: str) -> None:
        from .entity import apartment_device

        super().__init__(coordinator, f"last_unlock_{contract_id}")
        self._attr_translation_key = "last_unlock"
        self._contract_id = contract_id
        self._attr_device_info = apartment_device(contract_id, title)

    @property
    def native_value(self) -> str | None:
        rec = self.coordinator.last_unlock.get(self._contract_id)
        return rec["result"] if rec else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = self.coordinator.last_unlock.get(self._contract_id)
        return {k: v for k, v in rec.items() if k != "result"} if rec else None


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
            if (cd.lock or {}).get("locks") and f"unlock_{cid}" not in known:
                known.add(f"unlock_{cid}")
                ent = LastUnlockSensor(coordinator, cid, cd.title)
                ent.entity_id = "sensor." + slugify(f"msheireb {cd.title} last unlock")
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
