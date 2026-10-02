"""Real-unit regression (v0.3.6): fan not restored on turn-on while HA showed the requested speed.

A realistic AC model: power presses apply after a latency; while the AC is starting, a fan press is
echoed in the readings and then dropped (the AC comes up in Auto). Production timing constants are
used; time is driven by the frozen clock.
"""
import copy
from datetime import timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.components.persistent_notification import _async_get_or_create_notifications

from custom_components.msheireb.const import DOMAIN, FAN_ROLES
from custom_components.msheireb.models import parse_smart_home

from .fake_api import FakeApi

pytestmark = pytest.mark.real_sequencing

DATA = {"email": "me@example.com", "password": "pw", "access_token": "A", "refresh_token": "R", "expires_at": 9e9}
DINING = "climate.msheireb_demo01_dining_room"
LAST = "sensor.msheireb_demo01_dining_room_last_command"
KEY = "4242_501"
FAN_LABEL = {"auto": "HVAC Fan Auto", "low": "HVAC Fan Low", "medium": "HVAC Fan Medium", "high": "HVAC Fan High"}
SN_ROLE = {1: "power", 2: "auto", 3: "low", 4: "medium", 5: "high"}


class RealisticAc:
    """Dining Room AC behaviour (other rooms static)."""

    def __init__(self, clock, power=False, fan="auto", power_latency=7.0, startup=4.0):
        self.clock, self.power, self.fan = clock, power, fan
        self.power_latency, self.startup = power_latency, startup
        self.pending_power_at = None
        self.starting_until = None
        self.echo = None  # (fan, until): reading echoes a press the AC will drop
        self.ignore_fan = False
        self.extra_on = None  # inject a second ON fan reading (ambiguous controller state)
        self.resets_fan_on_start = True
        self.presses = []
        self.press_times = []  # (role, t)
        self.read_times = []
        self.drop_power = 0  # lose the next N power presses

    def _settle(self):
        now = self.clock()
        if self.pending_power_at is not None and now >= self.pending_power_at:
            self.power = not self.power
            self.pending_power_at = None
            if self.power:
                self.starting_until = now + self.startup
                if self.resets_fan_on_start:
                    self.fan = "auto"  # the unit comes up in Auto
        if self.echo and now >= self.echo[1]:
            self.echo = None

    def press(self, sn):
        self._settle()
        role = SN_ROLE[sn]
        self.presses.append(role)
        now = self.clock()
        self.press_times.append((role, now))
        if role == "power":
            if self.drop_power:
                self.drop_power -= 1
                return
            self.pending_power_at = now + self.power_latency
            return
        if self.ignore_fan:
            return
        if self.power and self.starting_until and now < self.starting_until:
            self.echo = (role, self.starting_until + 2.0)  # shown for a while, then gone
            return
        self.fan = role

    def apply(self, payload):
        self._settle()
        self.read_times.append(self.clock())
        dev = payload["rooms"][0]["devices"][0]["status"]
        shown = self.echo[0] if self.echo else self.fan
        for d in dev["digital"]:
            if d["label"] == "HVAC AC":
                d["current_status"] = "ON" if self.power else "OFF"
            for role, label in FAN_LABEL.items():
                if d["label"] == label:
                    d["current_status"] = "ON" if role == shown or role == self.extra_on else "OFF"
        return payload


@pytest.fixture
def ac(smart_home_payload, freezer):
    import time

    clock = time.monotonic
    model = RealisticAc(clock)
    FakeApi.instances.clear()
    FakeApi.payload = smart_home_payload
    FakeApi.controller = "connected"
    FakeApi.fail_login = FakeApi.fail_fetch = None
    FakeApi.ignore_commands = False
    FakeApi.drop_pulses = 0
    import custom_components.msheireb.coordinator as co
    real_sleep = co.asyncio.sleep

    async def clock_sleep(d):
        freezer.tick(timedelta(seconds=max(0.0, d)))
        await real_sleep(0)

    async def get_smart_home(self, cid):
        return model.apply(copy.deepcopy(self.state))

    async def send(self, ip, sn, type_io, type_code, value):
        self.commands.append({"sn": sn, "type_code": type_code})
        if sn in SN_ROLE and type_code == "D":
            model.press(sn)
        return {"ok": True}

    with patch("custom_components.msheireb.MsheirebApi", FakeApi), \
         patch.object(FakeApi, "async_get_smart_home", get_smart_home), \
         patch.object(FakeApi, "async_send_command", send), \
         patch.object(co.asyncio, "sleep", clock_sleep):
        yield model


async def _setup(hass, options=None):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="1263", entry_id="e1",
                            options={"pulse_interval": 1.0, **(options or {})})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data


async def _advance(hass, freezer, seconds, step=1.0):
    t = 0.0
    while t < seconds:
        freezer.tick(timedelta(seconds=step))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        t += step


async def _svc(hass, service, **data):
    await hass.services.async_call("climate", service, {"entity_id": DINING, **data}, blocking=True)
    await hass.async_block_till_done()


def _state(hass):
    s = hass.states.get(DINING)
    return s.state, s.attributes.get("fan_mode")


async def test_turn_on_waits_for_power_then_delay_then_fan(hass, ac, freezer):
    entry, coord = await _setup(hass)
    coord.drift.prev_fan[KEY] = "high"
    t0 = freezer.time_to_freeze if hasattr(freezer, "time_to_freeze") else None
    await _svc(hass, "turn_on")
    assert ac.presses == ["power", "high"]
    assert ac.power and ac.fan == "high"  # pressed after start-up -> kept
    await _advance(hass, freezer, 15)
    assert hass.states.get(LAST).state == "confirmed"
    assert coord.health.commands[KEY].fan_matches >= 2
    assert _state(hass) == ("cool", "high")


async def test_v036_timing_echo_then_drop_is_not_confirmed_and_is_retried(hass, ac, freezer):
    """Fan pressed right after power (delay 0, long start-up): the reading echoes High, then Auto."""
    ac.startup = 8.0  # power applies at +7 s, starting until +15 s; settle 10 s + delay 0 -> press at +10 s
    entry, coord = await _setup(hass, {"power_fan_delay": 0, "fan_settle": 5})
    coord.drift.prev_fan[KEY] = "high"
    await _svc(hass, "turn_on")
    assert ac.presses == ["power", "high"] and ac.echo is not None  # dropped by the starting AC
    await _advance(hass, freezer, 6)  # first verify read (after the 5 s fan settle) still shows the echo
    rec = coord.health.commands[KEY]
    assert rec.result == "pending"  # a single (echoed) reading never confirms
    await _advance(hass, freezer, 60)  # second read shows Auto -> mismatch -> fan-only retry
    assert ac.presses == ["power", "high", "high"]  # no extra power press
    assert rec.retries == 1 and rec.result == "confirmed" and ac.fan == "high"
    assert _state(hass) == ("cool", "high")


async def test_default_fan_settle_skips_the_echo(hass, ac, freezer):
    """Same slow start-up, default 10 s fan settle: the echo is gone before the first read."""
    ac.startup = 8.0
    entry, coord = await _setup(hass, {"power_fan_delay": 0})
    coord.drift.prev_fan[KEY] = "high"
    matches = _track_matches(coord)
    await _svc(hass, "turn_on")
    await _advance(hass, freezer, 90)
    rec = coord.health.commands[KEY]
    # the first counted read (>= 10 s after the press) already shows Auto -> retried, never confirmed on the echo
    assert ac.presses == ["power", "high", "high"] and rec.retries == 1
    assert rec.result == "confirmed" and ac.fan == "high"
    _assert_verification_timing(ac, matches, 10)


async def test_fan_never_applied_shows_actual_and_notifies(hass, ac, freezer):
    entry, coord = await _setup(hass, {"max_retries": 1})
    coord.drift.prev_fan[KEY] = "high"
    ac.ignore_fan = True
    await _svc(hass, "turn_on")
    assert _state(hass) == ("cool", "high")  # optimistic intent while the command is pending
    await _advance(hass, freezer, 120)
    rec = coord.health.commands[KEY]
    assert rec.result == "not_confirmed" and rec.retries == 1
    assert ac.presses == ["power", "high", "high"]
    assert _state(hass) == ("cool", "auto")  # the ACTUAL fan, not the requested one
    notes = {k: v for k, v in _async_get_or_create_notifications(hass).items() if k.startswith(DOMAIN)}
    assert any("command_" in k for k in notes)


async def test_turn_off_power_then_auto_after_delay_and_memory_kept(hass, ac, freezer):
    ac.power, ac.fan = True, "medium"
    entry, coord = await _setup(hass)
    await _svc(hass, "turn_off")
    assert ac.presses == ["power", "auto"]
    assert not ac.power and ac.fan == "auto"
    assert coord.drift.prev_fan[KEY] == "medium"
    await _advance(hass, freezer, 15)
    assert hass.states.get(LAST).state == "confirmed"
    assert _state(hass) == ("off", "off") and ac.fan == "auto"  # v0.3.18: fan_mode follows HVAC off
    # turning off again / drift / our own Auto never overwrite the remembered speed
    await _svc(hass, "turn_off")
    assert coord.drift.prev_fan[KEY] == "medium"
    await _svc(hass, "turn_on")
    await _advance(hass, freezer, 15)
    assert ac.fan == "medium" and _state(hass) == ("cool", "medium")


async def test_power_never_changes_no_fan_press(hass, ac, freezer):
    entry, coord = await _setup(hass, {"max_retries": 0})
    coord.drift.prev_fan[KEY] = "high"
    ac.power_latency = 10_000  # power press lost
    await _svc(hass, "turn_on")
    assert ac.presses == ["power"]  # waited up to 30 s for power; fan not pressed into an off AC
    await _advance(hass, freezer, 60)
    assert coord.health.commands[KEY].result == "not_confirmed"
    assert _state(hass) == ("off", "off")  # reverted to the actual state (off -> fan_mode off)


async def test_ambiguous_multi_on_reading_never_confirms(hass, ac, freezer):
    ac.power, ac.fan = True, "auto"
    entry, coord = await _setup(hass, {"max_retries": 0})
    ac.extra_on = "high"  # controller reports Auto ON *and* High ON
    zone = parse_smart_home(4242, ac.apply(copy.deepcopy(FakeApi.instances[-1].state)))[KEY]
    assert zone.fan_readings(FAN_ROLES) == {"auto": True, "low": False, "medium": False, "high": True}
    assert zone.fan_mode(FAN_ROLES) is None
    ac.ignore_fan = True
    await _svc(hass, "set_fan_mode", fan_mode="high")
    await _advance(hass, freezer, 60)
    assert coord.health.commands[KEY].result == "not_confirmed"  # never "confirmed" from Auto+High
    assert _state(hass)[1] is None  # shown as unknown, not as the requested High
    assert coord.drift.events == 0  # and no drift alarm from an ambiguous reading


async def test_debug_log_has_raw_fan_readings(hass, ac, freezer, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="custom_components.msheireb")
    ac.power, ac.fan = True, "low"
    await _setup(hass)
    assert any("fan readings={'auto': False, 'low': True, 'medium': False, 'high': False} -> fan_mode=low" in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------- 'Fan to Auto when off' disabled
OFF_MODE = {"fan_auto_when_off": False}


async def test_option_off_turn_off_sends_only_power_and_keeps_fan(hass, ac, freezer):
    ac.power, ac.fan = True, "medium"
    entry, coord = await _setup(hass, OFF_MODE)
    await _svc(hass, "turn_off")
    assert ac.presses == ["power"]  # no Fan Auto press
    assert coord.drift.desired[KEY] == {"power": False, "fan": "medium"}  # actual speed, no Auto expectation
    await _advance(hass, freezer, 15)  # power confirmed by the post-command refresh
    assert not ac.power and ac.fan == "medium"
    assert hass.states.get(LAST).state == "confirmed"
    assert _state(hass) == ("off", "off") and ac.fan == "medium"  # fan_mode follows HVAC off


async def test_option_off_turn_on_power_only_when_speed_kept(hass, ac, freezer):
    ac.power, ac.fan, ac.resets_fan_on_start = True, "medium", False
    entry, coord = await _setup(hass, OFF_MODE)
    await _svc(hass, "turn_off")
    await _advance(hass, freezer, 15)
    await _svc(hass, "turn_on")
    assert ac.presses == ["power", "power"]  # no fan press on/off
    await _advance(hass, freezer, 15)
    rec = coord.health.commands[KEY]
    assert rec.result == "confirmed" and "fan" not in rec.expected
    assert _state(hass) == ("cool", "medium")


async def test_option_off_turn_on_presses_fan_only_if_actual_differs(hass, ac, freezer):
    ac.power, ac.fan = True, "medium"  # this unit comes up in Auto after power-on
    entry, coord = await _setup(hass, OFF_MODE)
    await _svc(hass, "turn_off")
    await _advance(hass, freezer, 15)
    await _svc(hass, "turn_on")
    assert ac.presses == ["power", "power", "medium"]  # differs after power-on -> delay + verified fan
    assert ac.fan == "medium"
    await _advance(hass, freezer, 15)
    assert coord.health.commands[KEY].result == "confirmed"
    assert _state(hass) == ("cool", "medium")


async def test_option_off_fan_change_while_off_no_drift(hass, ac, freezer):
    ac.power, ac.fan = True, "medium"
    entry, coord = await _setup(hass, OFF_MODE)
    await _svc(hass, "turn_off")
    await _advance(hass, freezer, 15)
    ac.fan = "low"  # changed at the wall panel while off
    await _advance(hass, freezer, 700, step=10)
    assert coord.drift.events == 0 and ac.presses == ["power"]


async def test_option_on_default_unchanged(hass, ac, freezer):
    ac.power, ac.fan = True, "medium"
    entry, coord = await _setup(hass)  # default: Fan to Auto when off = on
    await _svc(hass, "turn_off")
    assert ac.presses == ["power", "auto"]


# ---------------------------------------------------------------- power settle time (real unit ~7 s)
def _first_read_after(ac, t):
    return min(r for r in ac.read_times if r > t) - t


async def test_settle_before_first_power_check_and_fan(hass, ac, freezer):
    entry, coord = await _setup(hass)  # defaults: settle 10 s, delay 5 s
    coord.drift.prev_fan[KEY] = "high"
    await _svc(hass, "turn_on")
    (_, t_power), (_, t_fan) = ac.press_times
    assert _first_read_after(ac, t_power) >= 10  # no power check during the settle time
    assert t_fan - t_power >= 15  # settle 10 s + Power -> fan delay 5 s
    await _advance(hass, freezer, 15)
    assert coord.health.commands[KEY].result == "confirmed"


async def test_settle_option_and_power_only_turn_off(hass, ac, freezer):
    ac.power, ac.fan = True, "medium"
    entry, coord = await _setup(hass, {"power_settle": 20, "fan_auto_when_off": False})
    await _svc(hass, "turn_off")
    (_, t_power), = ac.press_times
    assert _first_read_after(ac, t_power) >= 20
    assert not ac.power  # confirmed inside the command (after settle)
    await _advance(hass, freezer, 15)
    assert coord.health.commands[KEY].result == "confirmed"


async def test_power_never_changes_gives_up_after_settle_plus_30s(hass, ac, freezer):
    entry, coord = await _setup(hass, {"max_retries": 0})
    ac.power_latency = 10_000
    await _svc(hass, "turn_on")
    t_power = ac.press_times[0][1]
    reads = [r - t_power for r in ac.read_times if r > t_power]
    assert reads[0] >= 10 and 38 <= reads[-1] <= 42  # settle 10 s, then polls every 2 s for 30 s
    assert all(b - a >= 1.9 for a, b in zip(reads, reads[1:]))


async def test_retry_power_press_also_settles(hass, ac, freezer):
    entry, coord = await _setup(hass, {"max_retries": 1})
    coord.drift.prev_fan[KEY] = "high"
    ac.drop_power = 1  # first power press lost
    await _svc(hass, "turn_on")
    assert ac.presses == ["power"]  # power never changed -> fan not pressed
    await _advance(hass, freezer, 120, step=2)
    assert ac.presses == ["power", "power", "high"]  # retry: power, settle, delay, fan
    t_retry = ac.press_times[1][1]
    assert _first_read_after(ac, t_retry) >= 10
    assert ac.press_times[2][1] - t_retry >= 15
    assert coord.health.commands[KEY].result == "confirmed" and ac.fan == "high"


async def test_drift_restore_settles_before_fan(hass, ac, freezer):
    ac.power, ac.fan = True, "medium"
    entry, coord = await _setup(hass)
    await _svc(hass, "turn_off")  # desired: off + auto
    await _advance(hass, freezer, 20)
    ac.power, ac.fan = True, "low"  # turned on at the wall panel
    n = len(ac.presses)
    await _advance(hass, freezer, 700, step=10)  # 2 polls + grace -> restore
    assert ac.presses[n:] == ["power", "auto"]
    t_power, t_fan = ac.press_times[n][1], ac.press_times[n + 1][1]
    assert _first_read_after(ac, t_power) >= 10 and t_fan - t_power >= 15
    assert not ac.power and ac.fan == "auto"


# ---------------------------------------------------------------- fan settle time (fan is slow)
def _track_matches(coord):
    """Record (time, fan_matches) every time a matching fan read is counted."""
    import time as _t

    seen = []
    orig = coord._evaluate_commands

    def wrapper(data):
        before = {k: r.fan_matches for k, r in coord.health.commands.items()}
        orig(data)
        for k, r in coord.health.commands.items():
            if r.fan_matches > before.get(k, 0):
                seen.append((_t.monotonic(), r.fan_matches))

    coord._evaluate_commands = wrapper
    return seen


def _fan_presses(ac):
    return [t for role, t in ac.press_times if role != "power"]


def _assert_verification_timing(ac, matches, settle):
    """Every counted read 1 comes >= settle after the latest fan press, read 2 >= 5 s after read 1."""
    assert matches
    for i, (t, n) in enumerate(matches):
        press = max(p for p in _fan_presses(ac) if p <= t)
        if n == 1:
            assert t - press >= settle - 0.5, (t - press, settle)
        else:
            assert t - matches[i - 1][0] >= 4.5


async def test_fan_settle_direct_fan_change(hass, ac, freezer):
    ac.power, ac.fan = True, "low"
    entry, coord = await _setup(hass)  # default fan settle 10 s
    matches = _track_matches(coord)
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "high"},
                                   blocking=True)
    await hass.async_block_till_done()
    assert ac.presses == ["high"]
    rec = coord.health.commands[KEY]
    assert rec.confirm_timeout >= 1 + 20 + 10 + 5  # press + base + settle + 2nd read gap
    await _advance(hass, freezer, 9)
    assert rec.fan_matches == 0 and rec.result == "pending"  # nothing counted during the settle time
    await _advance(hass, freezer, 3)
    assert rec.fan_matches == 1 and rec.result == "pending"
    await _advance(hass, freezer, 6)
    assert rec.result == "confirmed" and rec.confirmed_after_s >= 15
    _assert_verification_timing(ac, matches, 10)
    assert _state(hass) == ("cool", "high")


async def test_fan_settle_option_long_is_not_flagged_early(hass, ac, freezer):
    ac.power, ac.fan = True, "low"
    entry, coord = await _setup(hass, {"fan_settle": 45, "max_retries": 0})
    matches = _track_matches(coord)
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "medium"},
                                   blocking=True)
    await hass.async_block_till_done()
    rec = coord.health.commands[KEY]
    await _advance(hass, freezer, 44)
    assert rec.result == "pending" and rec.fan_matches == 0
    assert _state(hass) == ("cool", "medium")  # intent kept while verifying
    await _advance(hass, freezer, 8)
    assert rec.result == "confirmed"
    _assert_verification_timing(ac, matches, 45)
    notes = {k for k in _async_get_or_create_notifications(hass) if k.startswith(DOMAIN)}
    assert not any("command_" in k for k in notes)


async def test_fan_settle_turn_off_auto_and_turn_on_restore(hass, ac, freezer):
    ac.power, ac.fan = True, "medium"
    entry, coord = await _setup(hass)
    matches = _track_matches(coord)
    await _svc(hass, "turn_off")
    assert ac.presses == ["power", "auto"]
    await _advance(hass, freezer, 20)
    assert coord.health.commands[KEY].result == "confirmed"
    await _svc(hass, "turn_on")
    assert ac.presses[2:] == ["power", "medium"]
    await _advance(hass, freezer, 20)
    assert coord.health.commands[KEY].result == "confirmed"
    assert len(matches) == 4
    _assert_verification_timing(ac, matches, 10)


async def test_fan_settle_on_retry_and_drift_restore(hass, ac, freezer):
    ac.power, ac.fan = True, "low"
    entry, coord = await _setup(hass, {"max_retries": 2})
    matches = _track_matches(coord)
    ac.ignore_fan = True  # first fan press lost by the AC
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "high"},
                                   blocking=True)
    await hass.async_block_till_done()
    ac.ignore_fan = False
    rec = coord.health.commands[KEY]
    await _advance(hass, freezer, 80)
    assert ac.presses == ["high", "high"] and rec.retries == 1 and rec.result == "confirmed"
    _assert_verification_timing(ac, matches, 10)
    # drift restore of the fan also waits the settle time before verifying
    matches.clear()
    ac.fan = "low"  # changed at the wall panel
    n = len(ac.presses)
    await _advance(hass, freezer, 700, step=10)
    assert ac.presses[n:] == ["high"] and ac.fan == "high"
    await _advance(hass, freezer, 30)
    assert coord.health.commands[KEY].result == "confirmed"
    _assert_verification_timing(ac, matches, 10)


async def test_v0318_speed_while_off_turns_on_with_power_fan_delay(hass, ac, freezer):
    """Fan speed chosen while off: power press, wait for the power change + 'Power -> fan delay', then the speed."""
    entry, coord = await _setup(hass)
    coord.drift.prev_fan[KEY] = "high"
    assert _state(hass) == ("off", "off")
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    assert ac.presses == ["power", "medium"]
    (_, t_power), (_, t_fan) = ac.press_times
    assert t_fan - t_power >= 10 + 5 - 0.01  # power settle (10 s, power applied at ~7 s) + 5 s delay
    assert ac.power and ac.fan == "medium"  # pressed after start-up -> kept
    await _advance(hass, freezer, 30)
    assert hass.states.get(LAST).state == "confirmed"
    assert _state(hass) == ("cool", "medium")
    assert coord.drift.prev_fan[KEY] == "medium"  # replaces the remembered High
