"""Door unlock button (disabled by default; also needs the 'Enable door unlock button' option)."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import MsheirebError
from .const import CONF_UNLOCK_ENABLED, DEFAULT_UNLOCK_ENABLED, UNLOCK_DURATION
from .coordinator import MsheirebCoordinator
from .entity import MsheirebEntity

_LOGGER = logging.getLogger(__name__)


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
        entry = self.coordinator.config_entry
        if not entry.options.get(CONF_UNLOCK_ENABLED, DEFAULT_UNLOCK_ENABLED):
            raise HomeAssistantError(
                "Door unlock is disabled. Turn on 'Enable door unlock button' in the "
                "Msheireb Smart Home options to use this button."
            )
        cd = self.contract_data
        if cd is None or not (cd.lock or {}).get("locks"):
            raise HomeAssistantError("No smart lock is configured for this apartment")
        _LOGGER.info("Door unlock requested from Home Assistant (%s)", UNLOCK_DURATION)
        record = {"at": dt_util.utcnow().isoformat(), "duration": UNLOCK_DURATION}
        try:
            await self.coordinator.api.async_unlock_door(self._contract_id, UNLOCK_DURATION)
        except MsheirebError as err:
            record |= {"result": "failed", "message": str(err)[:200]}
            self.coordinator.last_unlock[self._contract_id] = record
            self.coordinator.notify_health()
            _LOGGER.info("Door unlock failed: %s", err)
            raise HomeAssistantError(f"Door unlock failed: {err}") from err
        record |= {"result": "success", "message": None}
        self.coordinator.last_unlock[self._contract_id] = record
        self.coordinator.notify_health()
        _LOGGER.info("Door unlock accepted by the portal")
        await self.coordinator.async_request_refresh()  # refresh the lock status
