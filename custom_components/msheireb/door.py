"""Door unlock state machine, mirroring the portal's Unlock (5s) button.

Portal (web bundle): the button shows a spinner while the unlock mutation is pending, i.e.
POST /smart-lock/contract-access-point {contract_id, state: "unlock", duration: "5s"} and then, on
success, a refetch of GET /smart-lock/contract-status/{id} (awaited in onSuccess; a failed refetch
does not fail the unlock). No timer, websocket or push event is involved, and the status endpoint
reports only connected/outdated/low_battery, not locked/open. So: success = the POST returned 2xx.
HA shows 'unlocking' for at least MIN_UNLOCKING s (so it is visible), until UNLOCKED_DELAY after the portal accepts,
then 'unlocked' for UNLOCKED_FOR s (matching the real lock), then 'locked'.
"""
from __future__ import annotations

from collections.abc import Callable
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
DOOR_OPEN = "unlocked"
DOOR_FAILED = "failed"
DOOR_STATES = [DOOR_LOCKED, DOOR_UNLOCKING, DOOR_OPEN, DOOR_FAILED]
UNLOCKED_DELAY = 1.5  # s: the real lock takes ~1.5 s to release after the portal accepts
UNLOCKED_FOR = 6.0  # s: how long the real lock stays unlocked
MIN_UNLOCKING = 2.0  # s: 'unlocking' stays visible at least this long, even if the portal is faster
FAILED_HOLD = 10.0  # s: 'failed' is shown this long, then back to 'locked'


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
    problem: str | None = None  # portal message of the last failed unlock (cleared by a success)


class DoorManager:
    """locked -> unlocking (POST + status re-read, >= MIN_UNLOCKING) -> open (duration) -> locked;
    on failure: failed (FAILED_HOLD) -> locked. Every transition notifies the listeners at once."""

    def __init__(self, coordinator: MsheirebCoordinator) -> None:
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.doors: dict[int, DoorState] = {}
        self._timers: dict[int, CALLBACK_TYPE] = {}
        self._listeners: list[Callable[[], None]] = []

    def get(self, contract_id: int) -> DoorState:
        return self.doors.setdefault(contract_id, DoorState())

    @callback
    def async_add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return _remove

    @callback
    def _set(self, contract_id: int, state: str) -> None:
        self.get(contract_id).state = state
        _LOGGER.debug("Door state -> %s", state)
        self._notify()

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()  # entities write their state immediately
        self.coordinator.notify_health()

    @callback
    def _later(self, contract_id: int, delay: float, state: str, then: Callable[[], None] | None = None) -> None:
        self._cancel_timer(contract_id)

        @callback
        def _fire(_now: Any) -> None:
            self._timers.pop(contract_id, None)
            self._set(contract_id, state)
            if then is not None:
                then()

        self._timers[contract_id] = async_call_later(self.hass, max(0.0, delay), _fire)

    async def async_unlock(self, contract_id: int, source: str, entity_id: str | None = None) -> None:
        """Unlock under the safety gates; drives the Door sensor (the button is the only control)."""
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
        started = time.monotonic()
        self._set(contract_id, DOOR_UNLOCKING)  # visible before the request is even sent
        record: dict[str, Any] = {"at": dt_util.utcnow().isoformat(), "duration": duration}
        try:
            await coord.api.async_unlock_door(contract_id, duration)
        except MsheirebError as err:
            message = str(err)[:200]
            door.problem = message
            record |= {"result": "failed", "message": message}
            coord.last_unlock[contract_id] = record
            self._notify()  # attributes: result + message
            self._fire(contract_id, "failed", duration, message, source, entity_id)
            _LOGGER.info("Door unlock failed: %s", err)
            # keep 'unlocking' for the minimum time, then 'failed' for FAILED_HOLD, then 'locked'
            wait = MIN_UNLOCKING - (time.monotonic() - started)
            hold = lambda: self._later(contract_id, FAILED_HOLD, DOOR_LOCKED)  # noqa: E731
            if wait > 0:
                self._later(contract_id, wait, DOOR_FAILED, hold)
            else:
                self._set(contract_id, DOOR_FAILED)
                hold()
            raise HomeAssistantError(f"Door unlock failed: {err}") from err
        door.problem = None
        record |= {"result": "success", "message": None}
        coord.last_unlock[contract_id] = record
        self._notify()
        # like the portal: the spinner lasts until the lock status was re-read (errors ignored)
        try:
            status = await coord.api.async_get_lock_status(contract_id)
            if status:
                cd.lock = status
        except MsheirebError as err:
            _LOGGER.debug("Lock status refresh after unlock failed: %s", err)
        _LOGGER.info("Door unlock accepted by the portal")
        self._fire(contract_id, "success", duration, None, source, entity_id)
        open_for = UNLOCKED_FOR
        relock = lambda: self._later(contract_id, open_for, DOOR_LOCKED)  # noqa: E731
        wait = max(MIN_UNLOCKING - (time.monotonic() - started), UNLOCKED_DELAY)
        if wait > 0:
            self._later(contract_id, wait, DOOR_OPEN, relock)
        else:
            self._set(contract_id, DOOR_OPEN)
            relock()

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
