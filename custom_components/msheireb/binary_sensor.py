"""Binary sensors: apartment controller + door lock status (read-only)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MsheirebCoordinator
from .entity import MsheirebEntity
from .sensor import HealthEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MsheirebCoordinator = entry.runtime_data
    async_add_entities([PortalReachable(coordinator)])
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new: list[BinarySensorEntity] = []
        for cid, cd in (coordinator.data or {}).items():
            if cd.apartment_ip and f"ctl_{cid}" not in known:
                known.add(f"ctl_{cid}")
                new.append(ControllerConnected(coordinator, cid))
            for idx, _lock in enumerate((cd.lock or {}).get("locks") or []):
                for kind in ("connected", "battery"):
                    key = f"lock_{cid}_{idx}_{kind}"
                    if key not in known:
                        known.add(key)
                        new.append(LockSensor(coordinator, cid, idx, kind))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class ControllerConnected(MsheirebEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "controller"

    def __init__(self, coordinator: MsheirebCoordinator, contract_id: int) -> None:
        super().__init__(coordinator, contract_id)
        self._attr_unique_id = f"{contract_id}_controller_connected"

    @property
    def is_on(self) -> bool | None:
        cd = self.contract_data
        return cd.controller_connected if cd else None


class LockSensor(MsheirebEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MsheirebCoordinator, contract_id: int, idx: int, kind: str) -> None:
        super().__init__(coordinator, contract_id)
        self._idx = idx
        self._kind = kind
        self._attr_unique_id = f"{contract_id}_lock{idx}_{kind}"
        if kind == "connected":
            self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
            self._attr_translation_key = "lock_connected"
        else:
            self._attr_device_class = BinarySensorDeviceClass.BATTERY
            self._attr_translation_key = "lock_battery"

    def _lock(self) -> dict[str, Any] | None:
        cd = self.contract_data
        locks = ((cd.lock if cd else None) or {}).get("locks") or []
        return locks[self._idx] if self._idx < len(locks) else None

    @property
    def available(self) -> bool:
        return super().available and self._lock() is not None

    @property
    def is_on(self) -> bool | None:
        lock = self._lock()
        if lock is None:
            return None
        if self._kind == "connected":
            return bool((lock.get("current_lock_status") or {}).get("connected"))
        return bool(lock.get("low_battery"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lock = self._lock() or {}
        return {"lock_name": lock.get("display_name")}


class PortalReachable(HealthEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: MsheirebCoordinator) -> None:
        super().__init__(coordinator, "portal_reachable")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.health.portal_reachable
