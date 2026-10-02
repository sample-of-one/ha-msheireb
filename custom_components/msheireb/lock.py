"""Door lock entity (one per apartment with a smart lock). Disabled by default; unlocking also
needs the 'Enable door unlock' option. Locking is not supported: the door re-locks by itself."""
from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import UNLOCK_DURATION
from .coordinator import MsheirebCoordinator
from .door import DOOR_LOCKED, DOOR_OPEN, DOOR_UNLOCKING
from .entity import MsheirebEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MsheirebCoordinator = entry.runtime_data
    known: set[int] = set()

    @callback
    def _add_new() -> None:
        new = []
        for cid, cd in (coordinator.data or {}).items():
            if cid not in known and (cd.lock or {}).get("locks"):
                known.add(cid)
                ent = MsheirebDoorLock(coordinator, cid)
                ent.entity_id = "lock." + slugify(f"msheireb {cd.title} door")
                new.append(ent)
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class MsheirebDoorLock(MsheirebEntity, LockEntity):
    """locked -> unlocking (portal spinner) -> open for the unlock duration -> locked."""

    _attr_translation_key = "door"
    _attr_supported_features = LockEntityFeature.OPEN
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: MsheirebCoordinator, contract_id: int) -> None:
        super().__init__(coordinator, contract_id)
        self._attr_unique_id = f"{contract_id}_door_lock"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_health_listener(self.async_write_ha_state))

    @property
    def _door(self):
        return self.coordinator.door.get(self._contract_id)

    @property
    def available(self) -> bool:
        cd = self.contract_data
        return super().available and cd is not None and bool((cd.lock or {}).get("locks"))

    @property
    def is_locked(self) -> bool:
        return self._door.state == DOOR_LOCKED

    @property
    def is_unlocking(self) -> bool:
        return self._door.state == DOOR_UNLOCKING

    @property
    def is_open(self) -> bool:
        return self._door.state == DOOR_OPEN

    @property
    def is_jammed(self) -> bool:
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cd = self.contract_data
        locks = (cd.lock or {}).get("locks") or [] if cd else []
        first = locks[0] if locks else {}
        status = first.get("current_lock_status") or {}
        last = self.coordinator.last_unlock.get(self._contract_id) or {}
        return {
            "unlock_duration": UNLOCK_DURATION,
            "problem": self._door.problem,
            "last_result": last.get("result"),
            "last_unlock_at": last.get("at"),
            "lock_connected": status.get("connected"),
            "lock_outdated": status.get("outdated"),
            "low_battery": first.get("low_battery"),
        }

    async def async_unlock(self, **kwargs: Any) -> None:
        await self.coordinator.door.async_unlock(self._contract_id, "lock", self.entity_id)

    async def async_open(self, **kwargs: Any) -> None:
        await self.coordinator.door.async_unlock(self._contract_id, "lock", self.entity_id)

    async def async_lock(self, **kwargs: Any) -> None:
        raise HomeAssistantError(
            f"Locking is not supported: the door locks again by itself after the {UNLOCK_DURATION} unlock"
        )
