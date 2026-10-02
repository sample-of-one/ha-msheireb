"""Door unlock state machine, mirroring the portal's Unlock (5s) button.

Portal (web bundle): the button shows a spinner while the unlock mutation is pending, i.e.
POST /smart-lock/contract-access-point {contract_id, state: "unlock", duration: "5s"} and then, on
success, a refetch of GET /smart-lock/contract-status/{id} (awaited in onSuccess; a failed refetch
does not fail the unlock). No timer, websocket or push event is involved, and the status endpoint
reports only connected/outdated/low_battery, not locked/open. So: success = the POST returned 2xx;
the open window is the requested duration counted from that moment.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import MsheirebError
from .const import CONF_UNLOCK_ENABLED, DEFAULT_UNLOCK_ENABLED, DOMAIN, UNLOCK_DURATION

if TYPE_CHECKING:
    from .coordinator import MsheirebCoordinator

_LOGGER = logging.getLogger(__name__)

EVENT_DOOR_UNLOCKED = f"{DOMAIN}_door_unlocked"
DOOR_LOCKED = "locked"
DOOR_UNLOCKING = "unlocking"
DOOR_OPEN = "open"


def duration_seconds(duration: str) -> float:
    """'5s' -> 5.0, '500ms' -> 0.5, '1m' -> 60.0 (portal duration format)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m)?\s*", str(duration))
    if not m:
        return 5.0
    value, unit = float(m.group(1)), m.group(2) or "s"
    return value / 1000 if unit == "ms" else value * 60 if unit == "m" else value


@dataclass
class DoorState:
    state: str = DOOR_LOCKED
    open_until: float | None = None  # monotonic
    problem: str | None = None  # portal message of the last failed unlock


class DoorManager:
    def __init__(self, coordinator: MsheirebCoordinator) -> None:
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.doors: dict[int, DoorState] = {}
        self._timers: dict[int, CALLBACK_TYPE] = {}

    def get(self, contract_id: int) -> DoorState:
        return self.doors.setdefault(contract_id, DoorState())

    @callback
    def _set(self, contract_id: int, state: str) -> None:
        self.get(contract_id).state = state
        self.coordinator.notify_health()

    async def async_unlock(self, contract_id: int, source: str, entity_id: str | None = None) -> None:
        """Unlock under the safety gates; drives the Door lock entity, button and Last unlock sensor."""
        coord = self.coordinator
        if not coord.config_entry.options.get(CONF_UNLOCK_ENABLED, DEFAULT_UNLOCK_ENABLED):
            raise HomeAssistantError(
                "Door unlock is disabled. Turn on 'Enable door unlock' in the Msheireb Smart Home options."
            )
        cd = (coord.data or {}).get(contract_id)
        if cd is None or not (cd.lock or {}).get("locks"):
            raise HomeAssistantError("No smart lock is configured for this apartment")
        door = self.get(contract_id)
        if door.state == DOOR_UNLOCKING:
            raise HomeAssistantError("A door unlock is already in progress")
        duration = UNLOCK_DURATION
        _LOGGER.info("Door unlock requested from Home Assistant (%s, via %s)", duration, source)
        self._cancel_timer(contract_id)
        door.problem = None
        self._set(contract_id, DOOR_UNLOCKING)  # the portal's spinner
        record: dict[str, Any] = {"at": dt_util.utcnow().isoformat(), "duration": duration}
        try:
            await coord.api.async_unlock_door(contract_id, duration)
        except MsheirebError as err:
            message = str(err)[:200]
            door.problem = message
            record |= {"result": "failed", "message": message}
            coord.last_unlock[contract_id] = record
            self._set(contract_id, DOOR_LOCKED)
            self._fire(contract_id, "failed", duration, message, source, entity_id)
            _LOGGER.info("Door unlock failed: %s", err)
            raise HomeAssistantError(f"Door unlock failed: {err}") from err
        accepted = time.monotonic()
        record |= {"result": "success", "message": None}
        coord.last_unlock[contract_id] = record
        # like the portal: keep the spinner until the lock status has been re-read (errors ignored)
        try:
            status = await coord.api.async_get_lock_status(contract_id)
            if status:
                cd.lock = status
        except MsheirebError as err:
            _LOGGER.debug("Lock status refresh after unlock failed: %s", err)
        _LOGGER.info("Door unlock accepted by the portal")
        self._fire(contract_id, "success", duration, None, source, entity_id)
        remaining = accepted + duration_seconds(duration) - time.monotonic()
        if remaining <= 0:
            self._set(contract_id, DOOR_LOCKED)
            return
        door.open_until = accepted + duration_seconds(duration)
        self._set(contract_id, DOOR_OPEN)

        @callback
        def _relock(_now: Any) -> None:
            self._timers.pop(contract_id, None)
            self.get(contract_id).open_until = None
            self._set(contract_id, DOOR_LOCKED)

        self._timers[contract_id] = async_call_later(self.hass, remaining, _relock)

    @callback
    def _fire(self, contract_id: int, result: str, duration: str, message: str | None, source: str,
              entity_id: str | None) -> None:
        self.hass.bus.async_fire(EVENT_DOOR_UNLOCKED, {
            "contract_id": contract_id,
            "entity_id": entity_id,
            "result": result,
            "duration": duration,
            "message": message,
            "source": source,
        })

    def _cancel_timer(self, contract_id: int) -> None:
        if (cancel := self._timers.pop(contract_id, None)) is not None:
            cancel()

    @callback
    def async_cancel(self) -> None:
        for cid in list(self._timers):
            self._cancel_timer(cid)
