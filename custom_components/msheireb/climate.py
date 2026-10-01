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
    FAN_ROLES,
    OPTIMISTIC_TIMEOUT,
    CONF_PULSE_INTERVAL,
    DEFAULT_PULSE_INTERVAL,
    PULSES_BETWEEN_READS,
    ROLE_POWER,
    ROLE_TEMP_DOWN,
    ROLE_TEMP_UP,
    TEMP_STEP,
)
from .coordinator import MsheirebCoordinator
from .entity import MsheirebEntity
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
            names = [z.room_name for z in cd.zones.values()]
            for zone in sorted(cd.zones.values(), key=lambda z: z.sort_key):
                if zone.key in known:
                    continue
                known.add(zone.key)
                dup = names.count(zone.room_name) > 1
                new.append(MsheirebClimate(coordinator, entry, cid, zone, dup))
        if new:
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
        self._attr_unique_id = f"{contract_id}_{zone.device_id}_climate"
        self._attr_name = f"{zone.room_name} {zone.device_name}" if disambiguate else zone.room_name
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
            for name, (value, ts) in list(self._optimistic.items()):
                if actual.get(name) == value or now - ts > OPTIMISTIC_TIMEOUT:
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

    async def _pulse(self, zone: HvacZone, role: str, sent: list[dict[str, Any]]) -> None:
        sent.append(await self.coordinator.async_pulse(zone, role))

    async def _run_command(self, zone: HvacZone, description: str, expected: dict[str, Any], body) -> None:
        """Run pulses via body(sent_list); track the result for monitoring."""
        sent: list[dict[str, Any]] = []
        try:
            await body(sent)
        except MsheirebError as err:
            self.coordinator.track_command(zone, description, expected, sent, error=str(err))
            raise HomeAssistantError(f"{self.name}: command failed: {err}") from err
        if sent:
            self.coordinator.track_command(zone, description, expected, sent)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in (HVACMode.OFF, HVACMode.COOL):
            raise HomeAssistantError(f"Unsupported HVAC mode {hvac_mode}")
        want_on = hvac_mode == HVACMode.COOL
        async with self._lock, self.coordinator.command_lock(self._contract_id):
            zone = self._require_zone()
            if ROLE_POWER not in zone.controls:
                raise HomeAssistantError(f"{self.name}: no power control")
            if zone.power is want_on:
                self._optimistic.pop("power", None)
                self.async_write_ha_state()
                return

            async def body(sent: list[dict[str, Any]]) -> None:
                # Power is a toggle pulse; only sent when the reported state differs.
                await self._pulse(zone, ROLE_POWER, sent)

            await self._run_command(zone, f"set_hvac_mode {hvac_mode}", {"power": want_on}, body)
            self._set_opt("power", want_on)
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
            if zone.fan_mode(self._fan_roles) == fan_mode:
                self._optimistic.pop("fan", None)
                self.async_write_ha_state()
                return

            async def body(sent: list[dict[str, Any]]) -> None:
                await self._pulse(zone, fan_mode, sent)

            await self._run_command(zone, f"set_fan_mode {fan_mode}", {"fan": fan_mode}, body)
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
                self._optimistic.pop("target", None)
                self.async_write_ha_state()
                return
            role = ROLE_TEMP_UP if needed > 0 else ROLE_TEMP_DOWN
            if role not in zone.controls:
                raise HomeAssistantError(f"{self.name}: no {role} control")
            self._set_opt("target", target)
            cap = abs(needed)  # never send more pulses than initially needed

            async def body(sent: list[dict[str, Any]]) -> None:
                nonlocal zone
                remaining = cap
                while remaining > 0 and len(sent) < cap:
                    t_start = time.monotonic()
                    await self._pulse(zone, role, sent)
                    remaining -= 1
                    if remaining <= 0:
                        break
                    # spacing measured start-to-start (request time counts towards it)
                    await asyncio.sleep(max(0.0, self._pulse_interval - (time.monotonic() - t_start)))
                    if len(sent) % PULSES_BETWEEN_READS == 0:
                        fresh = await self.coordinator.async_read_zone(zone)
                        if fresh is not None and fresh.setpoint is not None:
                            zone = fresh
                            left = round((target - fresh.setpoint) / TEMP_STEP)
                            if left == 0 or (left > 0) != (needed > 0):
                                break  # reached or overshot
                            remaining = min(abs(left), cap - len(sent))

            await self._run_command(
                zone, f"set_temperature {current:.1f} -> {target:.1f}", {"target": target}, body
            )
            _LOGGER.debug("%s: %s pulse(s) towards %.1f", self.name, role, target)
        self.coordinator.schedule_refresh_after_command()
