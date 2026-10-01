"""Desired-state persistence and drift (external change) detection/restore."""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ADOPT_EXTERNAL,
    CONF_DRIFT_GRACE,
    CONF_EXTERNAL_CHANGE,
    DEFAULT_ADOPT_EXTERNAL,
    DEFAULT_DRIFT_GRACE,
    DEFAULT_EXTERNAL_CHANGE,
    DOMAIN,
    DRIFT_MIN_POLLS,
    EXT_IGNORE,
    EXT_NOTIFY,
    EXT_RESTORE,
    EXT_RESTORE_NOTIFY,
    FAN_ROLES,
    STORE_VERSION,
)

if TYPE_CHECKING:
    from .coordinator import MsheirebCoordinator, MsheirebData
    from .models import HvacZone

_LOGGER = logging.getLogger(__name__)

KEYS_ORDER = ("power", "target", "fan")


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.desired"


def actual_values(zone: HvacZone) -> dict[str, Any]:
    return {"power": zone.power, "target": zone.setpoint, "fan": zone.fan_mode(FAN_ROLES)}


def _differs(kind: str, want: Any, have: Any) -> bool:
    if have is None or want is None:
        return False  # unknown actual: never treat as drift
    if kind == "target":
        return abs(float(want) - float(have)) > 0.01
    return want != have


def describe(kind: str, value: Any) -> str:
    if kind == "target":
        return f"{float(value):.1f} °C"
    if kind == "power":
        return "on" if value else "off"
    return str(value)


LABELS = {"target": "setpoint", "power": "power", "fan": "fan"}


@dataclass
class Episode:
    since: float
    polls: int
    diff: dict[str, tuple[Any, Any]]  # kind -> (desired, actual)
    handled: bool = False
    restored: bool = False


@dataclass
class DriftEvent:
    room: str
    changes: dict[str, dict[str, Any]]
    action: str
    at: str


@dataclass
class DriftManager:
    hass: HomeAssistant
    coordinator: MsheirebCoordinator
    store: Store = field(init=False)
    desired: dict[str, dict[str, Any]] = field(default_factory=dict)
    auto_restore: dict[str, bool] = field(default_factory=dict)
    episodes: dict[str, Episode] = field(default_factory=dict)
    events: int = 0
    last_event: DriftEvent | None = None
    restoring: set[str] = field(default_factory=set)
    prev_fan: dict[str, str] = field(default_factory=dict)  # zone_key -> fan speed before HA turned it off

    def __post_init__(self) -> None:
        self.store = Store(self.hass, STORE_VERSION, store_key(self.coordinator.config_entry.entry_id))

    # ---------------------------------------------------------------- persistence
    async def async_load(self) -> None:
        data = await self.store.async_load() or {}
        self.desired = {k: dict(v) for k, v in (data.get("desired") or {}).items()}
        self.auto_restore = {k: bool(v) for k, v in (data.get("auto_restore") or {}).items()}
        self.prev_fan = {k: str(v) for k, v in (data.get("prev_fan") or {}).items() if v}

    def _data_to_save(self) -> dict[str, Any]:
        return {"desired": self.desired, "auto_restore": self.auto_restore, "prev_fan": self.prev_fan}

    @callback
    def _save(self) -> None:
        self.store.async_delay_save(self._data_to_save, 1)

    # ---------------------------------------------------------------- options
    @property
    def _opts(self) -> dict[str, Any]:
        return self.coordinator.config_entry.options

    @property
    def mode(self) -> str:
        return self._opts.get(CONF_EXTERNAL_CHANGE, DEFAULT_EXTERNAL_CHANGE)

    @property
    def grace(self) -> float:
        return float(self._opts.get(CONF_DRIFT_GRACE, DEFAULT_DRIFT_GRACE))

    @property
    def adopt(self) -> bool:
        return bool(self._opts.get(CONF_ADOPT_EXTERNAL, DEFAULT_ADOPT_EXTERNAL))

    # ---------------------------------------------------------------- state
    @callback
    def set_desired(self, zone_key: str, values: dict[str, Any]) -> None:
        self.desired.setdefault(zone_key, {}).update(values)
        self.episodes.pop(zone_key, None)  # a new intent ends any drift episode
        self.coordinator.alerts.clear(f"drift_{zone_key}")
        self._save()

    @callback
    def remember_fan(self, zone_key: str, fan: str | None) -> None:
        """Remember the fan speed to re-apply when HA turns the room back on."""
        if fan:
            self.prev_fan[zone_key] = fan
        else:
            self.prev_fan.pop(zone_key, None)
        self._save()

    @callback
    def pop_remembered_fan(self, zone_key: str) -> str | None:
        fan = self.prev_fan.pop(zone_key, None)
        if fan is not None:
            self._save()
        return fan

    @callback
    def forget_desired(self, zone_key: str, kind: str) -> None:
        if self.desired.get(zone_key, {}).pop(kind, None) is not None:
            self._save()

    def is_auto_restore(self, zone_key: str) -> bool:
        return self.auto_restore.get(zone_key, True)

    @callback
    def set_auto_restore(self, zone_key: str, on: bool) -> None:
        self.auto_restore[zone_key] = on
        self._save()
        self.coordinator.notify_health()

    @callback
    def mark_handled(self, zone_key: str) -> None:
        ep = self.episodes.get(zone_key)
        if ep is None:
            self.episodes[zone_key] = Episode(since=time.monotonic(), polls=0, diff={}, handled=True)
        else:
            ep.handled = True

    def _in_flight(self, zone: HvacZone) -> bool:
        rec = self.coordinator.health.commands.get(zone.key)
        if rec is not None and (rec.result == "pending" or rec.retrying):
            return True
        if zone.key in self.restoring:
            return True
        return self.coordinator.command_lock(zone.contract_id).locked()

    # ---------------------------------------------------------------- detection
    @callback
    def evaluate(self, data: MsheirebData) -> None:
        if self.mode == EXT_IGNORE:
            self.episodes.clear()
            return
        now = time.monotonic()
        for cd in data.values():
            if cd.controller_connected is False:
                continue  # can't act (and readings may be stale) while offline
            for key, zone in cd.zones.items():
                want = self.desired.get(key)
                if not want or self._in_flight(zone):
                    continue
                have = actual_values(zone)
                diff = {k: (want[k], have.get(k)) for k in KEYS_ORDER if k in want and _differs(k, want[k], have.get(k))}
                if want.get("power") is False and have.get("power") is False:
                    diff.pop("fan", None)  # fan speed is irrelevant while the room is off
                ep = self.episodes.get(key)
                if not diff:
                    if ep is not None:
                        if ep.restored:
                            self.coordinator.alerts.clear(f"drift_{key}")
                        self.episodes.pop(key, None)
                    continue
                if ep is None or (not ep.handled and set(ep.diff) != set(diff)):
                    ep = Episode(since=now, polls=1, diff=diff, handled=ep.handled if ep else False)
                    self.episodes[key] = ep
                else:
                    ep.polls += 1
                    ep.diff = diff
                if ep.handled:
                    continue
                if now - ep.since >= self.grace and ep.polls >= DRIFT_MIN_POLLS:
                    self._act(zone, ep)

    def _act(self, zone: HvacZone, ep: Episode) -> None:
        ep.handled = True
        self.events += 1
        mode = self.mode
        notify = mode in (EXT_NOTIFY, EXT_RESTORE_NOTIFY)
        want_restore = mode in (EXT_RESTORE, EXT_RESTORE_NOTIFY)
        if self.adopt:
            self.desired.setdefault(zone.key, {}).update({k: a for k, (_d, a) in ep.diff.items()})
            self._save()
            action = "adopted as the new desired state"
        elif want_restore and self.is_auto_restore(zone.key):
            ep.restored = True
            expected = {k: d for k, (d, _a) in ep.diff.items()}
            self.restoring.add(zone.key)
            self.coordinator.config_entry.async_create_task(
                self.hass, self._async_restore(zone, expected), f"{DOMAIN}_restore_{zone.key}"
            )
            action = "restoring: " + ", ".join(f"{LABELS[k]} → {describe(k, v)}" for k, v in expected.items())
        elif want_restore:
            action = "not restored (Auto-restore is off for this room)"
        else:
            action = "no action (notify only)"
        changes = {
            LABELS[k]: {"from": describe(k, d), "to": describe(k, a)} for k, (d, a) in ep.diff.items()
        }
        self.last_event = DriftEvent(room=zone.room_name, changes=changes, action=action, at=dt_util.utcnow().isoformat())
        _LOGGER.info("External change in %s: %s; %s", zone.room_name, changes, action)
        if notify:
            lines = "\n".join(f"- {name}: {c['from']} → {c['to']}" for name, c in changes.items())
            self.coordinator.alerts.raise_(
                f"drift_{zone.key}",
                f"Msheireb: {zone.room_name} changed outside Home Assistant",
                f"**{zone.room_name}** no longer matches the state last set from Home Assistant:\n{lines}\n\n"
                f"**Action:** {action}.",
            )
        self.coordinator.notify_health()

    async def _async_restore(self, zone: HvacZone, expected: dict[str, Any]) -> None:
        from .api import MsheirebError

        try:
            async with self.coordinator.command_lock(zone.contract_id):
                fresh = await self.coordinator.async_read_zone(zone) or zone
                await self.coordinator.async_command(
                    fresh, expected, "restore after external change",
                    self.coordinator.pulse_interval, set_desired=False,
                )
        except MsheirebError as err:
            _LOGGER.warning("Restore of %s failed: %s", zone.room_name, err)
        finally:
            self.restoring.discard(zone.key)
        self.coordinator.schedule_refresh_after_command()
