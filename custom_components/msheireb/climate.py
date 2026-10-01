"""Climate entities: one per room HVAC zone."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import MsheirebError
from .const import (
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    CONF_FAN_AUTO_WHEN_OFF,
    DEFAULT_FAN_AUTO_WHEN_OFF,
    FAN_ROLES,
    ROLE_FAN_AUTO,
    OPTIMISTIC_TIMEOUT,
    CONF_PULSE_INTERVAL,
    DEFAULT_PULSE_INTERVAL,
    ROLE_POWER,
    ROLE_TEMP_DOWN,
    ROLE_TEMP_UP,
    TEMP_STEP,
)
from .coordinator import KIND_FAN, KIND_POWER, KIND_TARGET, MsheirebCoordinator
from .entity import MsheirebEntity, room_device, room_entity_id, zone_needs_disambiguation
from .models import HvacZone

_LOGGER = logging.getLogger(__name__)


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
                if zone.key in known:
                    continue
                known.add(zone.key)
                dup = zone_needs_disambiguation(cd, zone)
                ent = MsheirebClimate(coordinator, entry, cid, zone, dup)
                ent.entity_id = room_entity_id("climate", cd, zone)
                new.append(ent)
        if new:
            _LOGGER.debug("Adding %d climate entit(ies): %s", len(new), [e.unique_id for e in new])
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


def _round_step(value: float) -> float:
    return round(round(value / TEMP_STEP) * TEMP_STEP, 1)


class MsheirebClimate(MsheirebEntity, ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = TEMP_STEP
    _attr_precision = 0.5
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL]
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self,
        coordinator: MsheirebCoordinator,
        entry: ConfigEntry,
        contract_id: int,
        zone: HvacZone,
        disambiguate: bool,
    ) -> None:
        super().__init__(coordinator, contract_id)
        self._zone_key = zone.key
        self._entry = entry
        self._attr_unique_id = f"{contract_id}_{zone.device_id}_climate"
        self._attr_name = None  # main feature of the room device -> entity name = room name
        self._attr_device_info = room_device(zone, disambiguate)
        self._attr_min_temp = float(entry.options.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP))
        self._attr_max_temp = float(entry.options.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP))
        self._pulse_interval = float(entry.options.get(CONF_PULSE_INTERVAL, DEFAULT_PULSE_INTERVAL))
        self._fan_roles = tuple(r for r in FAN_ROLES if r in zone.controls)
        self._attr_fan_modes = list(self._fan_roles)
        features = ClimateEntityFeature.TARGET_TEMPERATURE
        if self._fan_roles:
            features |= ClimateEntityFeature.FAN_MODE
        if ROLE_POWER in zone.controls:
            features |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        self._attr_supported_features = features
        # optimistic overrides: name -> (value, monotonic timestamp)
        self._optimistic: dict[str, tuple[Any, float]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ state
    @property
    def zone(self) -> HvacZone | None:
        cd = self.contract_data
        return cd.zones.get(self._zone_key) if cd else None

    @property
    def available(self) -> bool:
        cd = self.contract_data
        return (
            super().available
            and cd is not None
            and cd.controller_connected is True
            and self.zone is not None
        )

    def _opt(self, name: str, actual: Any) -> Any:
        item = self._optimistic.get(name)
        if item is None:
            return actual
        return item[0]

    def _set_opt(self, name: str, value: Any) -> None:
        self._optimistic[name] = (value, time.monotonic())
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        zone = self.zone
        now = time.monotonic()
        if zone is not None:
            actual = {
                "power": zone.power,
                "target": zone.setpoint,
                "fan": zone.fan_mode(self._fan_roles),
            }
            rec = self.coordinator.health.commands.get(self._zone_key)
            pending = rec is not None and (rec.result == "pending" or rec.retrying)
            for name, (value, ts) in list(self._optimistic.items()):
                if self._lock.locked() or self.coordinator.command_lock(self._contract_id).locked():
                    continue  # presses still running: keep showing the intent
                # actual wins as soon as it matches, the command is settled, or it is too old
                if actual.get(name) == value or not pending or now - ts > OPTIMISTIC_TIMEOUT:
                    self._optimistic.pop(name, None)
        super()._handle_coordinator_update()

    @property
    def current_temperature(self) -> float | None:
        zone = self.zone
        return zone.room_temperature if zone else None

    @property
    def target_temperature(self) -> float | None:
        zone = self.zone
        return self._opt("target", zone.setpoint if zone else None)

    @property
    def hvac_mode(self) -> HVACMode | None:
        zone = self.zone
        power = self._opt("power", zone.power if zone else None)
        if power is None:
            return None
        return HVACMode.COOL if power else HVACMode.OFF

    @property
    def fan_mode(self) -> str | None:
        zone = self.zone
        return self._opt("fan", zone.fan_mode(self._fan_roles) if zone else None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = self.zone
        if zone is None:
            return {}
        return {
            "room": zone.room_name,
            "device_id": zone.device_id,
            "control_sn": {role: c.sn for role, c in zone.controls.items()},
        }

    # ------------------------------------------------------------ commands
    def _require_zone(self) -> HvacZone:
        zone = self.zone
        if zone is None or not self.available:
            raise HomeAssistantError(f"{self.name}: controller not available")
        return zone

    async def _command(self, zone: HvacZone, expected: dict[str, Any], description: str,
                       fan_if_differs: bool = False) -> None:
        """Execute via the coordinator (which tracks, confirms, retries and records desired state)."""
        try:
            await self.coordinator.async_command(zone, expected, description, self._pulse_interval,
                                                 fan_if_differs=fan_if_differs)
        except MsheirebError as err:
            raise HomeAssistantError(f"{self.name}: command failed: {err}") from err

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """OFF: remember the fan speed, power off, fan to Auto. COOL: power on, re-apply the speed.

        Sequenced as one command (power first, then fan) under the apartment command lock;
        each part is only pulsed when the reported state differs, then confirmed/retried.
        """
        if hvac_mode not in (HVACMode.OFF, HVACMode.COOL):
            raise HomeAssistantError(f"Unsupported HVAC mode {hvac_mode}")
        want_on = hvac_mode == HVACMode.COOL
        drift = self.coordinator.drift
        async with self._lock, self.coordinator.command_lock(self._contract_id):
            zone = self._require_zone()
            if ROLE_POWER not in zone.controls:
                raise HomeAssistantError(f"{self.name}: no power control")
            current_fan = zone.fan_mode(self._fan_roles)
            expected: dict[str, Any] = {KIND_POWER: want_on}
            fan_auto = bool(self._entry.options.get(CONF_FAN_AUTO_WHEN_OFF, DEFAULT_FAN_AUTO_WHEN_OFF))
            keep_fan: str | None = None  # 'Fan to Auto when off' disabled: desired fan = actual
            if not want_on:
                memory = drift.prev_fan.get(zone.key)
                remembered = current_fan or drift.desired.get(zone.key, {}).get(KIND_FAN)
                # Only remember a real speed taken while on; never replace a remembered speed with
                # Auto (Auto may come from our own off-sequence, a drift restore or an AC restart).
                if remembered and (memory is None or (zone.power is not False and remembered != ROLE_FAN_AUTO)):
                    drift.remember_fan(zone.key, remembered)
                if fan_auto and ROLE_FAN_AUTO in self._fan_roles:
                    expected[KIND_FAN] = ROLE_FAN_AUTO
                elif not fan_auto:
                    keep_fan = current_fan  # power only; the fan speed is left untouched
            else:
                fan = drift.prev_fan.get(zone.key) or drift.desired.get(zone.key, {}).get(KIND_FAN)
                if fan not in self._fan_roles:
                    drift.forget_desired(zone.key, KIND_FAN)  # unknown: leave the fan as it is
                else:
                    # with 'Fan to Auto when off' disabled the speed is only pressed if the actual
                    # speed (read after the power change) differs from the remembered/desired one
                    expected[KIND_FAN] = fan
            if keep_fan:
                drift.set_desired(zone.key, {KIND_FAN: keep_fan})
            if self.coordinator._zone_matches(zone, expected):
                self.coordinator.async_set_desired(zone.key, expected)
                for name in ("power", "fan"):
                    self._optimistic.pop(name, None)
                self.async_write_ha_state()
                return
            # show the intent right away; the actual readings win after the command settles
            self._set_opt("power", want_on)
            if KIND_FAN in expected and fan_auto:
                self._set_opt("fan", expected[KIND_FAN])
            parts = ", ".join(f"{k}={v}" for k, v in expected.items())
            try:
                await self._command(zone, expected, f"set_hvac_mode {hvac_mode} ({parts})",
                                    fan_if_differs=want_on and not fan_auto)
            except HomeAssistantError:
                self._optimistic.pop("power", None)
                self._optimistic.pop("fan", None)
                self.async_write_ha_state()
                raise
        self.coordinator.schedule_refresh_after_command()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.COOL)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in self._fan_roles:
            raise HomeAssistantError(f"Unsupported fan mode {fan_mode}")
        async with self._lock, self.coordinator.command_lock(self._contract_id):
            zone = self._require_zone()
            # an explicit choice from HA is what to re-apply on the next turn-on (on or off)
            self.coordinator.drift.remember_fan(zone.key, fan_mode)
            if zone.fan_mode(self._fan_roles) == fan_mode:
                self.coordinator.async_set_desired(zone.key, {KIND_FAN: fan_mode})
                self._optimistic.pop("fan", None)
                self.async_write_ha_state()
                return
            await self._command(zone, {KIND_FAN: fan_mode}, f"set_fan_mode {fan_mode}")
            self._set_opt("fan", fan_mode)
        self.coordinator.schedule_refresh_after_command()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(mode)
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        target = _round_step(min(self.max_temp, max(self.min_temp, float(temp))))
        async with self._lock, self.coordinator.command_lock(self._contract_id):
            zone = self._require_zone()
            current = zone.setpoint
            if current is None:
                raise HomeAssistantError(f"{self.name}: current setpoint unknown")
            needed = round((target - current) / TEMP_STEP)
            if needed == 0:
                self.coordinator.async_set_desired(zone.key, {KIND_TARGET: target})
                self._optimistic.pop("target", None)
                self.async_write_ha_state()
                return
            role = ROLE_TEMP_UP if needed > 0 else ROLE_TEMP_DOWN
            if role not in zone.controls:
                raise HomeAssistantError(f"{self.name}: no {role} control")
            self._set_opt("target", target)
            await self._command(zone, {KIND_TARGET: target}, f"set_temperature {current:.1f} -> {target:.1f}")
        self.coordinator.schedule_refresh_after_command()
