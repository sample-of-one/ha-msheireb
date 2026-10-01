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
    entry, coord, api = await _setup(hass)
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
    """Without an option the climate entity uses the 2.0 s default (start-to-start)."""
    import custom_components.msheireb.climate as cl
    with patch.object(cl, "DEFAULT_PULSE_INTERVAL", 2.0):
        entry, coord, api = await _setup(hass)
    sleeps = []
    real_sleep = cl.asyncio.sleep

    async def fake_sleep(d):
        sleeps.append(d)
        await real_sleep(0)
    with patch.object(cl.asyncio, "sleep", fake_sleep):
        await hass.services.async_call("climate", "set_temperature", {"entity_id": DINING, "temperature": 20.5}, blocking=True)
    assert sleeps and all(1.5 < d <= 2.0 for d in sleeps)
