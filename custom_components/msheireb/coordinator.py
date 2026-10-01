"""DataUpdateCoordinator + health/command tracking for Msheireb."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .alerts import Alerts
from .api import (
    AUTH_FAILED,
    AUTH_OK,
    MsheirebApi,
    MsheirebAuthError,
    MsheirebConnectionError,
    MsheirebError,
    MsheirebNotFoundError,
)
from .const import (
    ALERT_AFTER,
    CMD_CONFIRMED,
    CMD_FAILED,
    CMD_NOT_CONFIRMED,
    CMD_PENDING,
    CONF_SCAN_INTERVAL,
    CONFIRM_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FAN_ROLES,
    PULSE_VALUE,
    REFRESH_AFTER_COMMAND,
)
from .models import HvacZone, parse_smart_home

_LOGGER = logging.getLogger(__name__)


@dataclass
class ContractData:
    contract: dict[str, Any]
    apartment_ip: str | None = None
    controller_connected: bool | None = None
    zones: dict[str, HvacZone] = field(default_factory=dict)
    lock: dict[str, Any] | None = None
    raw_smart_home: dict[str, Any] | None = None

    @property
    def contract_id(self) -> int:
        return int(self.contract["id"])

    @property
    def title(self) -> str:
        c = self.contract
        return str(c.get("unitId") or c.get("unitName") or c.get("externalId") or c["id"])


MsheirebData = dict[int, ContractData]


@dataclass
class CommandRecord:
    zone_key: str
    room: str
    description: str
    expected: dict[str, Any]
    pulses: list[dict[str, Any]]
    started_at: datetime
    sent_monotonic: float
    result: str = CMD_PENDING
    confirmed_after_s: float | None = None
    error: str | None = None

    def as_attributes(self) -> dict[str, Any]:
        return {
            "command": self.description,
            "expected": self.expected,
            "pulses_sent": len(self.pulses),
            "sent": [f"{p['label']} (sn {p['sn']})" for p in self.pulses][:30],
            "sent_at": self.started_at.isoformat(),
            "confirmed_after_s": self.confirmed_after_s,
            "error": self.error,
        }


@dataclass
class Health:
    auth_status: str = AUTH_OK
    last_success: datetime | None = None
    last_error: str | None = None
    last_error_type: str | None = None
    last_error_at: datetime | None = None
    portal_reachable: bool | None = None
    unreachable_since: float | None = None  # monotonic
    consecutive_failures: int = 0
    commands_sent: int = 0
    commands_confirmed: int = 0
    commands_failed: int = 0
    controller_down_since: dict[int, float] = field(default_factory=dict)  # monotonic
    commands: dict[str, CommandRecord] = field(default_factory=dict)  # zone_key -> last


class MsheirebCoordinator(DataUpdateCoordinator[MsheirebData]):
    """Polls smart-home + controller-status for every contract and tracks health."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: MsheirebApi) -> None:
        interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.api = api
        self.health = Health()
        self.alerts = Alerts(hass, entry)
        self._health_listeners: list[CALLBACK_TYPE] = []
        self._command_locks: dict[int, asyncio.Lock] = {}
        self._cancel_refresh: CALLBACK_TYPE | None = None
        self._cancel_confirm: dict[str, CALLBACK_TYPE] = {}

    # ---------------------------------------------------------------- health listeners
    @callback
    def async_add_health_listener(self, cb: CALLBACK_TYPE) -> Callable[[], None]:
        self._health_listeners.append(cb)

        def _remove() -> None:
            if cb in self._health_listeners:
                self._health_listeners.remove(cb)

        return _remove

    @callback
    def notify_health(self) -> None:
        for cb in list(self._health_listeners):
            cb()

    @callback
    def on_auth_status(self, status: str) -> None:
        """Called by the API client when its auth state changes."""
        self.health.auth_status = status
        self.notify_health()

    @property
    def token_expires(self) -> datetime | None:
        exp = getattr(self.api, "expires_at", None)
        return dt_util.utc_from_timestamp(exp) if exp else None

    @property
    def last_response_ms(self) -> float | None:
        return getattr(self.api, "last_response_ms", None)

    def command_lock(self, contract_id: int) -> asyncio.Lock:
        return self._command_locks.setdefault(contract_id, asyncio.Lock())

    # ---------------------------------------------------------------- polling
    async def _async_update_data(self) -> MsheirebData:
        try:
            contracts = await self.api.async_get_contracts()
            result: MsheirebData = {}
            for contract in contracts:
                cd = await self._fetch_contract(contract)
                if cd is not None:
                    result[cd.contract_id] = cd
        except MsheirebAuthError as err:
            self._record_failure(err, reachable=True)
            self.health.auth_status = AUTH_FAILED
            self.alerts.raise_reauth_issue()
            self.alerts.raise_(
                "auth",
                "Msheireb: login failed",
                "The Msheireb portal rejected the saved login and automatic re-login failed. "
                "Open **Settings → Devices & services → Msheireb Smart Home** and re-authenticate.",
            )
            self.notify_health()
            raise ConfigEntryAuthFailed(str(err)) from err
        except MsheirebConnectionError as err:
            self._record_failure(err, reachable=False)
            self.notify_health()
            raise UpdateFailed(str(err)) from err
        except MsheirebError as err:
            self._record_failure(err, reachable=True)
            self.notify_health()
            raise UpdateFailed(str(err)) from err
        self._record_success(result)
        self.notify_health()
        return result

    async def _fetch_contract(self, contract: dict[str, Any]) -> ContractData | None:
        cid = int(contract["id"])
        previous = (self.data or {}).get(cid)
        cd = ContractData(contract=contract)
        try:
            smart_home = await self.api.async_get_smart_home(cid)
        except MsheirebNotFoundError:
            _LOGGER.debug("Contract %s has no smart-home configuration", cid)
            return cd
        cd.raw_smart_home = smart_home
        cd.apartment_ip = (smart_home.get("apartment_ip") or "").strip() or None
        cd.zones = parse_smart_home(cid, smart_home)
        if cd.apartment_ip:
            try:
                status = await self.api.async_get_controller_status(cd.apartment_ip)
                cd.controller_connected = str(status.get("status", "")).lower() == "connected"
            except MsheirebAuthError:
                raise
            except MsheirebError as err:
                _LOGGER.debug("controller-status failed for %s: %s", cid, err)
                cd.controller_connected = previous.controller_connected if previous else None
        try:
            cd.lock = await self.api.async_get_lock_status(cid)
        except MsheirebAuthError:
            raise
        except MsheirebError:
            cd.lock = previous.lock if previous else None
        return cd

    def _record_failure(self, err: Exception, reachable: bool) -> None:
        h = self.health
        h.consecutive_failures += 1
        h.last_error = str(err)[:250] or type(err).__name__
        h.last_error_type = type(err).__name__
        h.last_error_at = dt_util.utcnow()
        h.portal_reachable = reachable
        now = time.monotonic()
        if reachable:
            h.unreachable_since = None
            self.alerts.clear("portal")
        else:
            if h.unreachable_since is None:
                h.unreachable_since = now
            if now - h.unreachable_since >= ALERT_AFTER:
                mins = int((now - h.unreachable_since) // 60)
                self.alerts.raise_(
                    "portal",
                    "Msheireb: portal unreachable",
                    f"The Msheireb portal API has been unreachable for about {mins} min. "
                    f"Last error: {h.last_error}",
                )

    def _record_success(self, data: MsheirebData) -> None:
        h = self.health
        h.consecutive_failures = 0
        h.last_success = dt_util.utcnow()
        h.portal_reachable = True
        h.unreachable_since = None
        h.auth_status = getattr(self.api, "auth_status", AUTH_OK) or AUTH_OK
        if h.auth_status == AUTH_FAILED:
            h.auth_status = AUTH_OK
        self.alerts.clear("portal")
        self.alerts.clear("auth")
        self.alerts.clear_reauth_issue()
        now = time.monotonic()
        for cid, cd in data.items():
            key = f"controller_{cid}"
            if cd.apartment_ip and cd.controller_connected is False:
                since = h.controller_down_since.setdefault(cid, now)
                if now - since >= ALERT_AFTER:
                    self.alerts.raise_(
                        key,
                        f"Msheireb: apartment controller offline ({cd.title})",
                        f"The apartment controller for {cd.title} has been disconnected for about "
                        f"{int((now - since) // 60)} min. AC control from Home Assistant is unavailable.",
                    )
            else:
                h.controller_down_since.pop(cid, None)
                self.alerts.clear(key)
        self._evaluate_commands(data)

    # ---------------------------------------------------------------- commands
    async def async_read_zone(self, zone: HvacZone) -> HvacZone | None:
        """Fresh read of one zone (used between temperature pulses)."""
        smart_home = await self.api.async_get_smart_home(zone.contract_id)
        fresh = parse_smart_home(zone.contract_id, smart_home).get(zone.key)
        if fresh is not None and self.data and zone.contract_id in self.data:
            self.data[zone.contract_id].zones[zone.key] = fresh
        return fresh

    async def async_pulse(self, zone: HvacZone, role: str) -> dict[str, Any]:
        """Send a PULSE for a role of a zone (sn discovered from labels)."""
        control = zone.controls.get(role)
        if control is None:
            raise MsheirebError(f"{zone.room_name}: no control for {role}")
        cd = (self.data or {}).get(zone.contract_id)
        if cd is None or not cd.apartment_ip:
            raise MsheirebError("apartment controller IP unknown")
        await self.api.async_send_command(
            cd.apartment_ip, control.sn, control.type_io, control.type_code, PULSE_VALUE
        )
        self.health.commands_sent += 1
        return {"sn": control.sn, "label": control.label, "type_code": control.type_code}

    @callback
    def track_command(
        self,
        zone: HvacZone,
        description: str,
        expected: dict[str, Any],
        pulses: list[dict[str, Any]],
        error: str | None = None,
    ) -> CommandRecord:
        rec = CommandRecord(
            zone_key=zone.key,
            room=zone.room_name,
            description=description,
            expected=expected,
            pulses=pulses,
            started_at=dt_util.utcnow(),
            sent_monotonic=time.monotonic(),
        )
        self.health.commands[zone.key] = rec
        if error is not None:
            rec.result = CMD_FAILED
            rec.error = error
            self.health.commands_failed += 1
            self._alert_command(rec)
        else:
            if zone.key in self._cancel_confirm:
                self._cancel_confirm.pop(zone.key)()

            @callback
            def _timeout(_now: Any) -> None:
                self._cancel_confirm.pop(zone.key, None)
                self.hass.async_create_task(self.async_refresh())

            self._cancel_confirm[zone.key] = async_call_later(self.hass, CONFIRM_TIMEOUT + 1, _timeout)
        self.notify_health()
        return rec

    @staticmethod
    def _zone_matches(zone: HvacZone, expected: dict[str, Any]) -> bool:
        for name, value in expected.items():
            if name == "target":
                if zone.setpoint is None or abs(zone.setpoint - value) > 0.01:
                    return False
            elif name == "power":
                if zone.power is not value:
                    return False
            elif name == "fan":
                if zone.fan_mode(FAN_ROLES) != value:
                    return False
        return True

    def _evaluate_commands(self, data: MsheirebData) -> None:
        now = time.monotonic()
        zones = {k: z for cd in data.values() for k, z in cd.zones.items()}
        for key, rec in self.health.commands.items():
            if rec.result != CMD_PENDING:
                continue
            zone = zones.get(key)
            if zone is not None and self._zone_matches(zone, rec.expected):
                rec.result = CMD_CONFIRMED
                rec.confirmed_after_s = round(now - rec.sent_monotonic, 1)
                self.health.commands_confirmed += 1
                self.alerts.clear(f"command_{key}")
                if key in self._cancel_confirm:
                    self._cancel_confirm.pop(key)()
            elif now - rec.sent_monotonic >= CONFIRM_TIMEOUT:
                rec.result = CMD_NOT_CONFIRMED
                self.health.commands_failed += 1
                self._alert_command(rec)

    def _alert_command(self, rec: CommandRecord) -> None:
        detail = f"error: {rec.error}" if rec.error else f"not confirmed within {int(CONFIRM_TIMEOUT)} s"
        self.alerts.raise_(
            f"command_{rec.zone_key}",
            f"Msheireb: AC command not applied ({rec.room})",
            f"`{rec.description}` for **{rec.room}** was {detail}. "
            f"Expected {rec.expected}; the AC may not have registered every press.",
        )

    # ---------------------------------------------------------------- scheduling
    @callback
    def schedule_refresh_after_command(self, delay: float = REFRESH_AFTER_COMMAND) -> None:
        if self._cancel_refresh:
            self._cancel_refresh()

        @callback
        def _fire(_now: Any) -> None:
            self._cancel_refresh = None
            self.hass.async_create_task(self.async_refresh())

        self._cancel_refresh = async_call_later(self.hass, delay, _fire)

    @callback
    def async_cancel_pending(self) -> None:
        if self._cancel_refresh:
            self._cancel_refresh()
            self._cancel_refresh = None
        for cancel in self._cancel_confirm.values():
            cancel()
        self._cancel_confirm.clear()
