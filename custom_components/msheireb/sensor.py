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

from .api import AUTH_FAILED, AUTH_OK, AUTH_REFRESHING, AUTH_RELOGIN
from .const import CMD_RESULTS, DOMAIN
from .coordinator import MsheirebCoordinator


def account_device(coordinator: MsheirebCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"account_{coordinator.config_entry.entry_id}")},
        name="Msheireb portal",
        manufacturer="Msheireb Properties",
        model="Resident portal API",
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

    def __init__(self, coordinator: MsheirebCoordinator, contract_id: int, zone_key: str, room: str, title: str) -> None:
        super().__init__(coordinator, f"last_command_{zone_key}")
        self._attr_translation_key = "last_command"
        self._attr_translation_placeholders = {"room": room}
        self._zone_key = zone_key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"contract_{contract_id}")},
            name=f"Msheireb {title}",
            manufacturer="Msheireb Properties",
            model="Smart apartment",
        )

    @property
    def native_value(self) -> str | None:
        rec = self.coordinator.health.commands.get(self._zone_key)
        return rec.result if rec else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = self.coordinator.health.commands.get(self._zone_key)
        return rec.as_attributes() if rec else None


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
            for zone in sorted(cd.zones.values(), key=lambda z: z.sort_key):
                if zone.key not in known:
                    known.add(zone.key)
                    new.append(LastCommandSensor(coordinator, cid, zone.key, zone.room_name, cd.title))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))
