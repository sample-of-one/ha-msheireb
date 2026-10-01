"""Climate entities: one per room HVAC zone."""
from __future__ import annotations

import asyncio  # noqa: F401  (tests patch climate.asyncio.sleep)
import logging
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
    TEMP_DEBOUNCE,
    CONF_PULSE_INTERVAL,
    DEFAULT_PULSE_INTERVAL,
    ROLE_POWER,
    ROLE_TEMP_DOWN,
    ROLE_TEMP_UP,
    TEMP_STEP,
)
from .coordinator import KIND_FAN, KIND_POWER, KIND_TARGET, MsheirebCoordinator, RoomSequence, SequenceSuperseded
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
        # requested state (power/fan/target) shown while the room's sequence + retries are in flight
        self._intent: dict[str, Any] = {}
        self._fan_if_differs = False

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
        return self._intent[name] if name in self._intent else actual

    @callback
    def _handle_coordinator_update(self) -> None:
        # the requested values win over every reading until the sequence and all its retries are
        # over; then the actual state is shown (after a final failure: the old values + a notification)
        if self._intent and not self.coordinator.room_busy(self._zone_key):
            self._intent.clear()
            self._fan_if_differs = False
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

    async def _launch(self, debounce: float = 0.0) -> None:
        """Show the request now and run the presses in the background (latest action wins)."""
        seq = self.coordinator.start_sequence(self._zone_key, self._run_sequence, debounce=debounce)
        self.async_write_ha_state()
        if self.coordinator.wait_for_sequences and seq.task is not None:  # test hook only
            await asyncio.shield(seq.task)

    async def _run_sequence(self, seq: RoomSequence) -> None:
        coord = self.coordinator
        queued = coord.command_lock(self._contract_id).locked()
        async with coord.command_lock(self._contract_id):
            if seq.cancelled:
                raise SequenceSuperseded
            seq.started = True
            zone = self.zone
            expected = {k: v for k, v in self._intent.items()}
            if zone is None or not expected:
                return
            if queued or zone.setpoint is None:
                # another sequence ran meanwhile: decide from a fresh reading, never toggle blindly
                zone = await coord.async_read_zone(zone) or zone
            if coord._zone_matches(zone, expected):
                coord.async_set_desired(zone.key, expected)
                return
            parts = ", ".join(f"{k}={v}" for k, v in expected.items())
            try:
                await coord.async_command(zone, expected, f"set ({parts})", self._pulse_interval,
                                          fan_if_differs=self._fan_if_differs)
            except MsheirebError as err:  # tracked as failed + notified by the coordinator
                _LOGGER.warning("%s: command failed: %s", self.name, err)
        coord.schedule_refresh_after_command()

    def _hvac_intent(self, hvac_mode: HVACMode, zone: HvacZone) -> None:
        """OFF: remember the fan speed, power off, fan to Auto. COOL: power on, re-apply the speed."""
        if hvac_mode not in (HVACMode.OFF, HVACMode.COOL):
            raise HomeAssistantError(f"Unsupported HVAC mode {hvac_mode}")
        if ROLE_POWER not in zone.controls:
            raise HomeAssistantError(f"{self.name}: no power control")
        want_on = hvac_mode == HVACMode.COOL
        drift = self.coordinator.drift
        current_fan = zone.fan_mode(self._fan_roles)
        fan_auto = bool(self._entry.options.get(CONF_FAN_AUTO_WHEN_OFF, DEFAULT_FAN_AUTO_WHEN_OFF))
        self._intent[KIND_POWER] = want_on
        self._fan_if_differs = False
        if not want_on:
            memory = drift.prev_fan.get(zone.key)
            remembered = current_fan or drift.desired.get(zone.key, {}).get(KIND_FAN)
            # Only remember a real speed taken while on; never replace a remembered speed with
            # Auto (Auto may come from our own off-sequence, a drift restore or an AC restart).
            if remembered and (memory is None or (zone.power is not False and remembered != ROLE_FAN_AUTO)):
                drift.remember_fan(zone.key, remembered)
            if fan_auto and ROLE_FAN_AUTO in self._fan_roles:
                self._intent[KIND_FAN] = ROLE_FAN_AUTO
            elif not fan_auto:
                self._intent.pop(KIND_FAN, None)
                if current_fan:
                    drift.set_desired(zone.key, {KIND_FAN: current_fan})  # power only; fan untouched
        else:
            fan = drift.prev_fan.get(zone.key) or drift.desired.get(zone.key, {}).get(KIND_FAN)
            if fan not in self._fan_roles:
                drift.forget_desired(zone.key, KIND_FAN)  # unknown: leave the fan as it is
                self._intent.pop(KIND_FAN, None)
            else:
                self._intent[KIND_FAN] = fan
                # with 'Fan to Auto when off' disabled the speed is only pressed if the actual
                # speed (read after the power change) differs from the remembered/desired one
                self._fan_if_differs = not fan_auto

    async def _start_if_needed(self, zone: HvacZone, debounce: float = 0.0) -> None:
        if not self.coordinator.room_busy(self._zone_key) and self.coordinator._zone_matches(zone, self._intent):
            self.coordinator.async_set_desired(zone.key, dict(self._intent))
            self._intent.clear()
            self._fan_if_differs = False
            self.async_write_ha_state()
            return
        await self._launch(debounce)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Returns at once; power first, then fan, run in the background through the apartment queue,
        each part only pulsed when the reported state differs, then confirmed/retried."""
        zone = self._require_zone()
        self._hvac_intent(hvac_mode, zone)
        await self._start_if_needed(zone)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.COOL)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in self._fan_roles:
            raise HomeAssistantError(f"Unsupported fan mode {fan_mode}")
        zone = self._require_zone()
        # an explicit choice from HA is what to re-apply on the next turn-on (on or off)
        self.coordinator.drift.remember_fan(zone.key, fan_mode)
        self._intent[KIND_FAN] = fan_mode
        self._fan_if_differs = False
        await self._start_if_needed(zone)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        mode = kwargs.get(ATTR_HVAC_MODE)
        if temp is None and mode is None:
            return
        zone = self._require_zone()
        if mode is not None:
            self._hvac_intent(mode, zone)
        debounce = 0.0
        if temp is not None:
            target = _round_step(min(self.max_temp, max(self.min_temp, float(temp))))
            current = zone.setpoint
            if current is None:
                raise HomeAssistantError(f"{self.name}: current setpoint unknown")
            needed = round((target - current) / TEMP_STEP)
            if needed:
                role = ROLE_TEMP_UP if needed > 0 else ROLE_TEMP_DOWN
                if role not in zone.controls:
                    raise HomeAssistantError(f"{self.name}: no {role} control")
            self._intent[KIND_TARGET] = target
            debounce = TEMP_DEBOUNCE  # rapid +/- taps: one target, pressed after the taps stop
        await self._start_if_needed(zone, debounce)
