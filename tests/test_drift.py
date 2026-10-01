"""Desired-state persistence and drift (external change) handling."""
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.persistent_notification import _async_get_or_create_notifications

from custom_components.msheireb.const import DOMAIN
from custom_components.msheireb.drift import store_key

from .fake_api import FakeApi

DATA = {"email": "me@example.com", "password": "pw", "access_token": "A", "refresh_token": "R", "expires_at": 9e9}
DINING = "climate.msheireb_demo01_dining_room"
KEY = "4242_501"
SWITCH = "switch.msheireb_demo01_dining_room_auto_restore"
EVENTS = "sensor.msheireb_portal_external_change_events"


@pytest.fixture(autouse=True)
def fake_api(smart_home_payload):
    FakeApi.instances.clear()
    FakeApi.payload = smart_home_payload
    FakeApi.controller = "connected"
    FakeApi.fail_login = FakeApi.fail_fetch = None
    FakeApi.ignore_commands = False
    FakeApi.drop_pulses = 0
    with patch("custom_components.msheireb.MsheirebApi", FakeApi), \
         patch("custom_components.msheireb.climate.DEFAULT_PULSE_INTERVAL", 0), \
         patch("custom_components.msheireb.coordinator.REFRESH_AFTER_COMMAND", 0):
        yield


async def _setup(hass, options=None):
    opts = {"pulse_interval": 0.5, **(options or {})}
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="1263", options=opts, entry_id="e1")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data, FakeApi.instances[-1]


def _notes(hass):
    return {k: v for k, v in _async_get_or_create_notifications(hass).items() if k.startswith(DOMAIN)}


def _dining(api):
    dev = api.state["rooms"][0]["devices"][0]["status"]
    return {a["label"]: a for a in dev["analog"]}, {d["label"]: d for d in dev["digital"]}


def external_setpoint(api, raw):
    _dining(api)[0]["HVAC Setpoint"]["current_status"] = raw


def external_power(api, on):
    _dining(api)[1]["HVAC AC"]["current_status"] = "ON" if on else "OFF"


async def _poll(hass, coord, age=None):
    if age is not None and KEY in coord.drift.episodes:
        coord.drift.episodes[KEY].since -= age
    await coord.async_refresh()
    await hass.async_block_till_done()


async def _set_temp(hass, t):
    await hass.services.async_call("climate", "set_temperature", {"entity_id": DINING, "temperature": t}, blocking=True)


@pytest.fixture
def fast_sleep():
    import custom_components.msheireb.coordinator as co
    real = co.asyncio.sleep

    async def fs(d):
        await real(0)
    with patch.object(co.asyncio, "sleep", fs):
        yield


async def test_desired_persisted_in_store(hass, hass_storage, fast_sleep):
    entry, coord, api = await _setup(hass)
    await _set_temp(hass, 21.0)
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "high"}, blocking=True)
    assert coord.drift.desired[KEY] == {"target": 21.0, "fan": "high"}
    await coord.drift.store.async_save(coord.drift._data_to_save())
    assert hass_storage[store_key("e1")]["data"]["desired"][KEY]["target"] == 21.0


async def test_desired_restored_after_restart_and_drift_restored(hass, hass_storage, fast_sleep):
    hass_storage[store_key("e1")] = {"version": 1, "minor_version": 1, "key": store_key("e1"),
                                    "data": {"desired": {KEY: {"target": 21.0, "power": True}}, "auto_restore": {}}}
    entry, coord, api = await _setup(hass)
    assert coord.drift.desired[KEY]["target"] == 21.0  # loaded; actual is 19.5 -> drift
    assert api.commands == []  # first poll only starts the episode
    await _poll(hass, coord, age=61)
    assert [c["sn"] for c in api.commands] == [6, 6, 6]  # restored 19.5 -> 21.0 via Temp Up
    await _poll(hass, coord)
    assert hass.states.get(DINING).attributes["temperature"] == 21.0


async def test_grace_period_and_two_polls(hass, fast_sleep):
    entry, coord, api = await _setup(hass)
    await _set_temp(hass, 20.0)
    await _poll(hass, coord)  # confirm command
    api.commands.clear()
    external_setpoint(api, 2400)  # wall panel -> 24.0
    await _poll(hass, coord)  # poll 1: episode starts
    await _poll(hass, coord)  # poll 2 but < 60 s
    assert api.commands == [] and not _notes(hass)
    await _poll(hass, coord, age=61)  # sustained > 60 s and >= 2 polls
    assert [c["sn"] for c in api.commands] == [7] * 8  # 24.0 -> 20.0
    notes = _notes(hass)
    msg = notes[f"{DOMAIN}_e1_drift_{KEY}"]["message"]
    assert "Dining Room" in notes[f"{DOMAIN}_e1_drift_{KEY}"]["title"]
    assert "setpoint: 20.0 °C → 24.0 °C" in msg and "restoring" in msg
    s = hass.states.get(EVENTS)
    assert s.state == "1" and s.attributes["last_action"].startswith("restoring")
    await _poll(hass, coord)  # back in sync -> notification dismissed
    assert not _notes(hass)


async def test_zero_grace_still_needs_two_polls(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0})
    await _set_temp(hass, 20.0)
    await _poll(hass, coord)
    api.commands.clear()
    external_setpoint(api, 2100)
    await _poll(hass, coord)
    assert api.commands == []
    await _poll(hass, coord)
    assert [c["sn"] for c in api.commands] == [7, 7]


async def test_power_outage_restores_power_once(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0})
    await hass.services.async_call("climate", "turn_on", {"entity_id": DINING}, blocking=True)  # already on: records desired
    assert coord.drift.desired[KEY] == {"power": True}
    external_power(api, False)
    await _poll(hass, coord); await _poll(hass, coord)
    assert [c["sn"] for c in api.commands] == [1]
    await _poll(hass, coord); await _poll(hass, coord)
    assert [c["sn"] for c in api.commands] == [1]  # no repeated toggling
    assert hass.states.get(DINING).state == "cool"


async def test_notify_only(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0, "external_change": "notify"})
    await _set_temp(hass, 20.0); await _poll(hass, coord); api.commands.clear()
    external_setpoint(api, 2300)
    await _poll(hass, coord); await _poll(hass, coord)
    assert api.commands == []
    assert "notify only" in next(iter(_notes(hass).values()))["message"]
    await _poll(hass, coord)
    assert hass.states.get(EVENTS).state == "1"  # one event per episode


async def test_restore_without_notify(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0, "external_change": "restore"})
    await _set_temp(hass, 20.0); await _poll(hass, coord); api.commands.clear()
    external_setpoint(api, 2050)
    await _poll(hass, coord); await _poll(hass, coord)
    assert [c["sn"] for c in api.commands] == [7]
    assert not _notes(hass)


async def test_ignore_mode(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0, "external_change": "ignore"})
    await _set_temp(hass, 20.0); await _poll(hass, coord); api.commands.clear()
    external_setpoint(api, 2300)
    for _ in range(3):
        await _poll(hass, coord)
    assert api.commands == [] and not _notes(hass) and hass.states.get(EVENTS).state == "0"


async def test_auto_restore_switch_off(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0})
    assert hass.states.get(SWITCH).state == "on"
    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    assert hass.states.get(SWITCH).state == "off"
    await _set_temp(hass, 20.0); await _poll(hass, coord); api.commands.clear()
    external_setpoint(api, 2300)
    await _poll(hass, coord); await _poll(hass, coord)
    assert api.commands == []
    assert "Auto-restore is off" in next(iter(_notes(hass).values()))["message"]


async def test_adopt_external_changes(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0, "adopt_external": True})
    await _set_temp(hass, 20.0); await _poll(hass, coord); api.commands.clear()
    external_setpoint(api, 2300)
    await _poll(hass, coord); await _poll(hass, coord)
    assert api.commands == []
    assert coord.drift.desired[KEY]["target"] == 23.0
    assert "adopted" in next(iter(_notes(hass).values()))["message"]


async def test_no_drift_while_command_in_flight(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0})
    FakeApi.ignore_commands = True  # command stays pending (not confirmed yet)
    await _set_temp(hass, 21.0)
    api.commands.clear()
    await _poll(hass, coord); await _poll(hass, coord)
    assert api.commands == [] and KEY not in coord.drift.episodes


async def test_no_drift_without_desired(hass):
    entry, coord, api = await _setup(hass, {"drift_grace": 0})
    external_setpoint(api, 2300)
    await _poll(hass, coord); await _poll(hass, coord)
    assert api.commands == [] and hass.states.get(EVENTS).state == "0"


async def test_failed_command_not_repeated_by_drift(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0, "max_retries": 0})
    FakeApi.ignore_commands = True
    await _set_temp(hass, 21.0)
    rec = coord.health.commands[KEY]
    rec.sent_monotonic -= rec.confirm_timeout + 1
    await _poll(hass, coord)
    assert rec.result == "not_confirmed"
    api.commands.clear()
    await _poll(hass, coord); await _poll(hass, coord)
    assert api.commands == []  # drift doesn't silently retry a failed command


async def test_switch_state_persisted(hass, hass_storage):
    entry, coord, api = await _setup(hass)
    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    await coord.drift.store.async_save(coord.drift._data_to_save())
    assert hass_storage[store_key("e1")]["data"]["auto_restore"][KEY] is False
