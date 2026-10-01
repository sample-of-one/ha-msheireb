"""Monitoring sensors, notifications, repair issue, diagnostics."""
import json
import time
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.persistent_notification import _async_get_or_create_notifications
from homeassistant.helpers import issue_registry as ir

from custom_components.msheireb.api import MsheirebAuthError, MsheirebConnectionError
from custom_components.msheireb.const import DOMAIN
from custom_components.msheireb.diagnostics import async_get_config_entry_diagnostics

from .fake_api import FakeApi

DATA = {"email": "me@example.com", "password": "secretpw", "access_token": "ACCESSTOK",
        "refresh_token": "REFRESHTOK", "expires_at": 9e9}
DINING = "climate.msheireb_demo01_dining_room"


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
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="1263", options=options or {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data, FakeApi.instances[-1]


def _notes(hass):
    return {k: v for k, v in _async_get_or_create_notifications(hass).items() if k.startswith(DOMAIN)}


def st(hass, eid):
    return hass.states.get(eid)


async def test_health_sensors_after_setup(hass):
    await _setup(hass)
    assert st(hass, "sensor.msheireb_portal_auth_status").state == "ok"
    assert st(hass, "sensor.msheireb_portal_consecutive_failures").state == "0"
    assert st(hass, "sensor.msheireb_portal_api_response_time").state == "123.4"
    assert st(hass, "sensor.msheireb_portal_last_successful_update").state not in ("unknown", "unavailable")
    assert st(hass, "sensor.msheireb_portal_token_expiry").state not in ("unknown", "unavailable")
    assert st(hass, "binary_sensor.msheireb_portal_portal_reachable").state == "on"
    assert st(hass, "sensor.msheireb_portal_commands_sent").state == "0"
    assert st(hass, "sensor.msheireb_demo01_dining_room_last_command").state == "unknown"
    # existing sensors kept
    assert st(hass, "binary_sensor.msheireb_demo01_apartment_controller").state == "on"
    assert st(hass, "binary_sensor.msheireb_demo01_door_lock_connection").state == "on"


async def test_command_confirmed_and_counters(hass):
    entry, coord, api = await _setup(hass)
    await hass.services.async_call("climate", "set_temperature", {"entity_id": DINING, "temperature": 20.5}, blocking=True)
    s = st(hass, "sensor.msheireb_demo01_dining_room_last_command")
    assert s.state == "pending"
    assert s.attributes["pulses_sent"] == 2 and s.attributes["expected"] == {"target": 20.5}
    await coord.async_refresh(); await hass.async_block_till_done()
    s = st(hass, "sensor.msheireb_demo01_dining_room_last_command")
    assert s.state == "confirmed" and s.attributes["confirmed_after_s"] is not None
    assert st(hass, "sensor.msheireb_portal_commands_sent").state == "2"
    assert st(hass, "sensor.msheireb_portal_commands_confirmed").state == "1"
    assert st(hass, "sensor.msheireb_portal_commands_failed").state == "0"


async def test_command_not_confirmed_notifies_then_clears(hass):
    entry, coord, api = await _setup(hass, options={"max_retries": 0})
    FakeApi.ignore_commands = True
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "low"}, blocking=True)
    coord.health.commands["4242_501"].sent_monotonic -= 30  # pretend 30 s passed
    await coord.async_refresh(); await hass.async_block_till_done()
    assert st(hass, "sensor.msheireb_demo01_dining_room_last_command").state == "not_confirmed"
    assert st(hass, "sensor.msheireb_portal_commands_failed").state == "1"
    notes = _notes(hass)
    assert any("command_4242_501" in k for k in notes)
    assert "Dining Room" in next(iter(notes.values()))["title"]
    # a later successful command in the same room clears it
    FakeApi.ignore_commands = False
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "medium"}, blocking=True)
    await coord.async_refresh(); await hass.async_block_till_done()
    assert st(hass, "sensor.msheireb_demo01_dining_room_last_command").state == "confirmed"
    assert not _notes(hass)


async def test_portal_unreachable_after_5_min(hass):
    entry, coord, api = await _setup(hass)
    FakeApi.fail_fetch = MsheirebConnectionError("timeout")
    await coord.async_refresh(); await coord.async_refresh(); await hass.async_block_till_done()
    assert st(hass, "binary_sensor.msheireb_portal_portal_reachable").state == "off"
    assert st(hass, "sensor.msheireb_portal_consecutive_failures").state == "2"
    err = st(hass, "sensor.msheireb_portal_last_error")
    assert "timeout" in err.state and err.attributes["type"] == "MsheirebConnectionError"
    assert not _notes(hass)  # < 5 min: no notification yet
    coord.health.unreachable_since -= 301
    await coord.async_refresh(); await hass.async_block_till_done()
    assert any(k.endswith("_portal") for k in _notes(hass))
    FakeApi.fail_fetch = None
    await coord.async_refresh(); await hass.async_block_till_done()
    assert not _notes(hass)
    assert st(hass, "binary_sensor.msheireb_portal_portal_reachable").state == "on"
    assert st(hass, "sensor.msheireb_portal_consecutive_failures").state == "0"


async def test_controller_offline_after_5_min(hass):
    entry, coord, api = await _setup(hass)
    FakeApi.controller = "disconnected"
    await coord.async_refresh(); await hass.async_block_till_done()
    assert not _notes(hass)
    coord.health.controller_down_since[4242] -= 301
    await coord.async_refresh(); await hass.async_block_till_done()
    assert any(k.endswith("controller_4242") for k in _notes(hass))
    FakeApi.controller = "connected"
    await coord.async_refresh(); await hass.async_block_till_done()
    assert not _notes(hass)


async def test_auth_failure_notification_and_repair_issue(hass):
    entry, coord, api = await _setup(hass)
    FakeApi.fail_fetch = MsheirebAuthError("expired")
    await coord.async_refresh(); await hass.async_block_till_done()
    assert st(hass, "sensor.msheireb_portal_auth_status").state == "failed"
    assert any(k.endswith("_auth") for k in _notes(hass))
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"reauth_{entry.entry_id}") is not None
    FakeApi.fail_fetch = None
    await coord.async_refresh(); await hass.async_block_till_done()
    assert not _notes(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"reauth_{entry.entry_id}") is None
    assert st(hass, "sensor.msheireb_portal_auth_status").state == "ok"


async def test_auth_status_callback_updates_sensor(hass):
    entry, coord, api = await _setup(hass)
    api._status_cb("refreshing")
    await hass.async_block_till_done()
    assert st(hass, "sensor.msheireb_portal_auth_status").state == "refreshing"


async def test_notifications_can_be_disabled(hass):
    entry, coord, api = await _setup(hass, options={"notifications": False})
    FakeApi.controller = "disconnected"
    await coord.async_refresh()
    coord.health.controller_down_since[4242] -= 301
    await coord.async_refresh(); await hass.async_block_till_done()
    assert not _notes(hass)


async def test_pulse_interval_option_used(hass):
    entry, coord, api = await _setup(hass, options={"pulse_interval": 0.05})
    sleeps = []
    import custom_components.msheireb.climate as cl
    real_sleep = cl.asyncio.sleep

    async def fake_sleep(d):
        sleeps.append(d)
        await real_sleep(0)
    with patch.object(cl.asyncio, "sleep", fake_sleep):
        await hass.services.async_call("climate", "set_temperature", {"entity_id": DINING, "temperature": 20.5}, blocking=True)
    assert sleeps and all(0 <= d <= 0.05 for d in sleeps)


async def test_diagnostics_redacted(hass):
    entry, coord, api = await _setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag, default=str)
    for secret in ("secretpw", "ACCESSTOK", "REFRESHTOK", "me@example.com", "10.0.0.10", "DEMO01", "L1"):
        assert secret not in text, secret
    assert diag["contracts"][0]["zones"]["4242_501"]["controls"]["temp_up"]["sn"] == 6
    assert diag["health"]["auth_status"] == "ok"


async def test_default_spacing_used_when_no_option(hass):
    """Without an option the climate entity uses the 5.0 s default (start-to-start)."""
    import custom_components.msheireb.climate as cl
    from custom_components.msheireb.const import DEFAULT_PULSE_INTERVAL
    assert DEFAULT_PULSE_INTERVAL == 5.0
    with patch.object(cl, "DEFAULT_PULSE_INTERVAL", 5.0):
        entry, coord, api = await _setup(hass)
    sleeps = []
    real_sleep = cl.asyncio.sleep

    async def fake_sleep(d):
        sleeps.append(d)
        await real_sleep(0)
    with patch.object(cl.asyncio, "sleep", fake_sleep):
        await hass.services.async_call("climate", "set_temperature", {"entity_id": DINING, "temperature": 20.5}, blocking=True)
    assert sleeps and all(4.5 < d <= 5.0 for d in sleeps)


async def test_confirm_timeout_scales_with_pulses(hass):
    """4 presses at 5 s spacing -> window 4*5+20 = 40 s from the first press."""
    entry, coord, api = await _setup(hass, options={"pulse_interval": 5.0, "max_retries": 0})
    FakeApi.ignore_commands = True
    import custom_components.msheireb.climate as cl
    real_sleep = cl.asyncio.sleep

    async def fast_sleep(d):
        await real_sleep(0)
    with patch.object(cl.asyncio, "sleep", fast_sleep):
        await hass.services.async_call("climate", "set_temperature",
                                       {"entity_id": DINING, "temperature": 21.5}, blocking=True)  # 19.5 -> 21.5
    rec = coord.health.commands["4242_501"]
    assert len(rec.pulses) == 4 and rec.confirm_timeout == 40.0
    s = st(hass, "sensor.msheireb_demo01_dining_room_last_command")
    assert s.attributes["confirm_timeout_s"] == 40.0
    rec.sent_monotonic -= 30  # 30 s: past the old fixed 20 s, inside the 40 s window
    await coord.async_refresh(); await hass.async_block_till_done()
    assert st(hass, "sensor.msheireb_demo01_dining_room_last_command").state == "pending"
    assert not _notes(hass)
    rec.sent_monotonic -= 11  # 41 s total
    await coord.async_refresh(); await hass.async_block_till_done()
    assert st(hass, "sensor.msheireb_demo01_dining_room_last_command").state == "not_confirmed"
    assert "40 s" in next(iter(_notes(hass).values()))["message"]


async def test_single_pulse_window_is_spacing_plus_20(hass):
    entry, coord, api = await _setup(hass, options={"pulse_interval": 5.0})
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "low"}, blocking=True)
    assert coord.health.commands["4242_501"].confirm_timeout == 25.0



# ------------------------------------------------------------------ retries
LAST = "sensor.msheireb_demo01_dining_room_last_command"


async def _expire(hass, coord, key="4242_501", extra=1.0):
    rec = coord.health.commands[key]
    rec.sent_monotonic -= rec.confirm_timeout + extra
    await coord.async_refresh()
    await hass.async_block_till_done()
    return rec


async def test_temperature_retry_sends_only_missing_pulses(hass):
    entry, coord, api = await _setup(hass)
    FakeApi.drop_pulses = 2  # AC misses 2 of 3 presses: 19.5 -> 20.0 instead of 21.0
    await hass.services.async_call("climate", "set_temperature", {"entity_id": DINING, "temperature": 21.0}, blocking=True)
    assert [c["sn"] for c in api.commands] == [6, 6, 6]
    await coord.async_refresh(); await hass.async_block_till_done()
    assert st(hass, LAST).state == "pending"  # 20.0 != 21.0, window not over
    api.commands.clear()
    rec = await _expire(hass, coord)
    assert [c["sn"] for c in api.commands] == [6, 6]  # remaining delta from actual 20.0 only
    assert rec.retries == 1 and rec.confirm_timeout == 20.0  # spacing 0 in tests: 2*0+20
    await coord.async_refresh(); await hass.async_block_till_done()
    s = st(hass, LAST)
    assert s.state == "confirmed" and s.attributes["retries"] == 1 and s.attributes["pulses_sent"] == 5
    assert st(hass, "sensor.msheireb_portal_command_retries").state == "1"
    assert st(hass, "sensor.msheireb_portal_commands_failed").state == "0"
    assert not _notes(hass)


async def test_retry_window_scales_with_retry_pulses(hass):
    entry, coord, api = await _setup(hass, options={"pulse_interval": 5.0})
    import custom_components.msheireb.climate as cl
    real_sleep = cl.asyncio.sleep

    async def fast_sleep(d):
        await real_sleep(0)
    FakeApi.drop_pulses = 3
    with patch.object(cl.asyncio, "sleep", fast_sleep):
        await hass.services.async_call("climate", "set_temperature", {"entity_id": DINING, "temperature": 21.0}, blocking=True)
        rec = coord.health.commands["4242_501"]
        assert rec.confirm_timeout == 35.0  # 3*5+20
        FakeApi.drop_pulses = 1  # retry: 1 of 3 missed
        await _expire(hass, coord)
        assert rec.retries == 1 and rec.confirm_timeout == 35.0  # 3 retry presses
        await _expire(hass, coord)  # second retry: only 1 press missing
        assert rec.retries == 2 and rec.confirm_timeout == 25.0  # 1*5+20
    await coord.async_refresh(); await hass.async_block_till_done()
    assert st(hass, LAST).state == "confirmed"


async def test_power_retry_never_blindly_toggles(hass):
    entry, coord, api = await _setup(hass)
    FakeApi.drop_pulses = 1
    await hass.services.async_call("climate", "turn_off", {"entity_id": DINING}, blocking=True)
    assert [c["sn"] for c in api.commands] == [1, 2]  # power (dropped by the AC), then fan auto
    # meanwhile the AC was switched off at the wall panel
    for d in api.state["rooms"][0]["devices"][0]["status"]["digital"]:
        if d["label"] == "HVAC AC":
            d["current_status"] = "OFF"
    api.commands.clear()
    # expire without a poll first seeing it (simulate by expiring directly via retry path)
    rec = coord.health.commands["4242_501"]
    rec.sent_monotonic -= rec.confirm_timeout + 1
    await coord._async_retry(rec)
    assert api.commands == []  # state already matches -> no toggle
    assert rec.result == "confirmed" and rec.retries == 0


async def test_power_retry_resends_when_still_different(hass):
    entry, coord, api = await _setup(hass)
    FakeApi.drop_pulses = 1
    await hass.services.async_call("climate", "turn_off", {"entity_id": DINING}, blocking=True)
    api.commands.clear()
    rec = await _expire(hass, coord)
    assert [c["sn"] for c in api.commands] == [1] and rec.retries == 1
    await coord.async_refresh(); await hass.async_block_till_done()
    assert st(hass, LAST).state == "confirmed"
    assert st(hass, DINING).state == "off"


async def test_fan_retry(hass):
    entry, coord, api = await _setup(hass)
    FakeApi.drop_pulses = 1
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "low"}, blocking=True)
    api.commands.clear()
    await _expire(hass, coord)
    assert [c["sn"] for c in api.commands] == [3]
    await coord.async_refresh(); await hass.async_block_till_done()
    assert st(hass, LAST).state == "confirmed"


async def test_final_failure_only_after_last_retry(hass):
    entry, coord, api = await _setup(hass)  # default max_retries = 2
    FakeApi.ignore_commands = True
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "low"}, blocking=True)
    await _expire(hass, coord)
    assert st(hass, LAST).state == "pending" and not _notes(hass)
    await _expire(hass, coord)
    assert st(hass, LAST).state == "pending" and not _notes(hass)
    rec = await _expire(hass, coord)
    s = st(hass, LAST)
    assert s.state == "not_confirmed" and s.attributes["retries"] == 2 and s.attributes["pulses_sent"] == 3
    assert st(hass, "sensor.msheireb_portal_command_retries").state == "2"
    assert st(hass, "sensor.msheireb_portal_commands_failed").state == "1"
    notes = _notes(hass)
    assert len(notes) == 1 and "2 retries" in next(iter(notes.values()))["message"]


async def test_newer_command_supersedes_retry(hass):
    entry, coord, api = await _setup(hass)
    FakeApi.ignore_commands = True
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "low"}, blocking=True)
    old = coord.health.commands["4242_501"]
    FakeApi.ignore_commands = False
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "medium"}, blocking=True)
    api.commands.clear()
    await coord._async_retry(old)
    assert api.commands == []  # old command no longer current


async def test_max_retries_option_in_flow(hass):
    entry, coord, api = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    key = next(k for k in result["data_schema"].schema if k == "max_retries")
    assert key.default() == 2
