"""Per-room 'Auto-restore' switch (restore the HA-set state after external changes)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MsheirebCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MsheirebCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = []
        for cid, cd in (coordinator.data or {}).items():
            for zone in sorted(cd.zones.values(), key=lambda z: z.sort_key):
                if zone.key not in known:
                    known.add(zone.key)
                    new.append(AutoRestoreSwitch(coordinator, cid, zone.key, zone.room_name, cd.title))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class AutoRestoreSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "auto_restore"
    _attr_icon = "mdi:backup-restore"

    def __init__(self, coordinator: MsheirebCoordinator, contract_id: int, zone_key: str, room: str, title: str) -> None:
        self.coordinator = coordinator
        self._zone_key = zone_key
        self._attr_unique_id = f"{zone_key}_auto_restore"
        self._attr_translation_placeholders = {"room": room}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"contract_{contract_id}")},
            name=f"Msheireb {title}",
            manufacturer="Msheireb Properties",
            model="Smart apartment",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_health_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self.coordinator.drift.is_auto_restore(self._zone_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "desired": self.coordinator.drift.desired.get(self._zone_key),
            "on_external_change": self.coordinator.drift.mode,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.drift.set_auto_restore(self._zone_key, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.drift.set_auto_restore(self._zone_key, False)
