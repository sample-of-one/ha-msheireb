"""Door unlock button (disabled by default; also needs the 'Enable door unlock' option)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MsheirebCoordinator
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
                new.append(UnlockDoorButton(coordinator, cid))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class UnlockDoorButton(MsheirebEntity, ButtonEntity):
    """Sends the portal's temporary unlock for the apartment's door (one per contract)."""

    _attr_translation_key = "unlock_door"
    _attr_icon = "mdi:lock-open-variant"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: MsheirebCoordinator, contract_id: int) -> None:
        super().__init__(coordinator, contract_id)
        self._attr_unique_id = f"{contract_id}_door_unlock"

    async def async_press(self) -> None:
        # same state machine as the Door lock entity (locked -> unlocking -> open -> locked)
        await self.coordinator.door.async_unlock(self._contract_id, "button", self.entity_id)
