"""Turn off -> remember fan speed + fan Auto; turn on -> re-apply the remembered speed."""
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
POWER, AUTO, LOW, MED, HIGH = 1, 2, 3, 4, 5  # Dining control sn's in the fixture


@pytest.fixture(autouse=True)
def fake_api(smart_home_payload):
    FakeApi.instances.clear()
    FakeApi.payload = smart_home_payload
    FakeApi.controller = "connected"
    FakeApi.fail_login = FakeApi.fail_fetch = None
    FakeApi.ignore_commands = False
    FakeApi.drop_pulses = 0
    import custom_components.msheireb.coordinator as co
    real_sleep = co.asyncio.sleep

    async def fast_sleep(_d):
        await real_sleep(0)

    with patch("custom_components.msheireb.MsheirebApi", FakeApi), \
         patch("custom_components.msheireb.coordinator.REFRESH_AFTER_COMMAND", 0), \
         patch.object(co.asyncio, "sleep", fast_sleep):
        yield


async def _setup(hass, payload=None):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="1263", options={"pulse_interval": 0.5},
                            entry_id="e1")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data, FakeApi.instances[-1]


def _digital(api):
    return {d["label"]: d for d in api.state["rooms"][0]["devices"][0]["status"]["digital"]}


def _set_fan_at_panel(api, label):
    for k, d in _digital(api).items():
        if k.startswith("HVAC Fan"):
            d["current_status"] = "ON" if k == label else "OFF"


async def _svc(hass, service, **data):
    await hass.services.async_call("climate", service, {"entity_id": DINING, **data}, blocking=True)


async def _poll(hass, coord, age=None):
    if age is not None and KEY in coord.drift.episodes:
        coord.drift.episodes[KEY].since -= age
    await coord.async_refresh()
    await hass.async_block_till_done()


def _sns(api):
    return [c["sn"] for c in api.commands]


async def test_off_remembers_fan_and_sets_auto_then_on_restores(hass, hass_storage):
    entry, coord, api = await _setup(hass)  # fixture: on, fan high
    await _svc(hass, "turn_off")
    assert _sns(api) == [POWER, AUTO]  # power first, then fan
    assert coord.drift.prev_fan[KEY] == "high"
    assert coord.drift.desired[KEY] == {"power": False, "fan": "auto"}
    await _poll(hass, coord)
    s = hass.states.get(DINING)
    assert s.state == "off" and s.attributes["fan_mode"] == "off"  # v0.3.18: follows HVAC off
    assert coord.data[4242].zones[KEY].fan_mode(("auto", "low", "medium", "high")) == "auto"  # actual fan
    assert coord.health.commands[KEY].result == "confirmed"
    await coord.drift.store.async_save(coord.drift._data_to_save())
    assert hass_storage[store_key("e1")]["data"]["prev_fan"] == {KEY: "high"}

    api.commands.clear()
    await _svc(hass, "set_hvac_mode", hvac_mode="cool")
    assert _sns(api) == [POWER, HIGH]
    assert coord.drift.desired[KEY] == {"power": True, "fan": "high"}
    assert coord.drift.prev_fan[KEY] == "high"  # kept (only replaced by a real speed)
    await _poll(hass, coord)
    s = hass.states.get(DINING)
    assert s.state == "cool" and s.attributes["fan_mode"] == "high"
    assert coord.health.commands[KEY].result == "confirmed"


async def test_off_when_fan_already_auto_sends_only_power(hass):
    entry, coord, api = await _setup(hass)
    _set_fan_at_panel(api, "HVAC Fan Auto")
    await _poll(hass, coord)
    await _svc(hass, "turn_off")
    assert _sns(api) == [POWER]
    assert coord.drift.prev_fan[KEY] == "auto"
    api.commands.clear()
    await _poll(hass, coord)
    await _svc(hass, "turn_on")
    assert _sns(api) == [POWER]  # remembered auto == current auto -> no fan press


async def test_turn_off_again_while_off_keeps_memory(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "turn_off")
    await _poll(hass, coord)
    api.commands.clear()
    await _svc(hass, "turn_off")  # already off + auto
    assert api.commands == []
    assert coord.drift.prev_fan[KEY] == "high"


async def test_remembered_fan_survives_restart(hass, hass_storage):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "turn_off")
    await _poll(hass, coord)
    await coord.drift.store.async_save(coord.drift._data_to_save())
    state = api.state
    assert await hass.config_entries.async_unload(entry.entry_id)
    FakeApi.payload = state  # the AC stays off + auto across the HA restart
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coord, api = entry.runtime_data, FakeApi.instances[-1]
    assert coord.drift.prev_fan[KEY] == "high"
    await _svc(hass, "turn_on")
    assert _sns(api) == [POWER, HIGH]


async def test_fan_change_while_off_is_not_drift(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "turn_off")
    await _poll(hass, coord)
    api.commands.clear()
    _set_fan_at_panel(api, "HVAC Fan Low")  # changed at the wall panel / by the AC while off
    for _ in range(3):
        await _poll(hass, coord, age=120)
    assert api.commands == []
    assert coord.drift.events == 0 and KEY not in coord.drift.episodes
    assert not {k for k in _async_get_or_create_notifications(hass) if k.startswith(DOMAIN)}
    # but turning it ON at the panel is still an external change (power) -> restored
    _digital(api)["HVAC AC"]["current_status"] = "ON"
    await _poll(hass, coord)
    await _poll(hass, coord, age=61)
    assert coord.drift.events == 1
    assert _sns(api) == [POWER, AUTO]  # restored to the desired off state: power off, fan auto
    assert coord.drift.prev_fan[KEY] == "high"  # memory untouched by the restore


async def test_fan_set_from_ha_while_off_turns_on_at_that_speed(hass):
    """v0.3.18: a speed chosen while off turns the room on at that speed (was: stays off)."""
    entry, coord, api = await _setup(hass)
    await _svc(hass, "turn_off")
    await _poll(hass, coord)
    api.commands.clear()
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    assert _sns(api) == [POWER, MED]  # power on, then the chosen speed
    assert coord.drift.prev_fan[KEY] == "medium"  # replaces the remembered High
    assert coord.drift.desired[KEY] == {"power": True, "fan": "medium"}
    await _poll(hass, coord)
    s = hass.states.get(DINING)
    assert s.state == "cool" and s.attributes["fan_mode"] == "medium"


async def test_on_falls_back_to_desired_fan_or_leaves_it(hass):
    entry, coord, api = await _setup(hass)
    _digital(api)["HVAC AC"]["current_status"] = "OFF"  # off at the panel, no memory
    _set_fan_at_panel(api, "HVAC Fan Auto")
    await _poll(hass, coord)
    coord.drift.desired[KEY] = {"fan": "low"}
    await _svc(hass, "turn_on")
    assert _sns(api) == [POWER, LOW]  # fallback: last desired fan

    # nothing known: fan left as it is, and not part of the desired state
    await _poll(hass, coord)
    _digital(api)["HVAC AC"]["current_status"] = "OFF"
    await _poll(hass, coord)
    coord.drift.desired[KEY] = {}
    coord.drift.prev_fan.clear()
    api.commands.clear()
    await _svc(hass, "turn_on")
    assert _sns(api) == [POWER]
    assert "fan" not in coord.drift.desired[KEY]


async def test_off_command_unconfirmed_fan_is_retried(hass):
    entry, coord, api = await _setup(hass)
    FakeApi.drop_pulses = 0
    # the AC registers the power press but misses the fan press
    orig = api.async_send_command

    async def drop_fan_once(ip, sn, *a):
        if sn == AUTO and not getattr(drop_fan_once, "done", False):
            drop_fan_once.done = True
            api.commands.append({"sn": sn})
            return {"ok": True}
        return await orig(ip, sn, *a)

    api.async_send_command = drop_fan_once
    await _svc(hass, "turn_off")
    rec = coord.health.commands[KEY]
    rec.sent_monotonic -= rec.confirm_timeout + 1
    api.commands.clear()
    await _poll(hass, coord)  # window over -> retry re-reads state, sends only the fan press
    await hass.async_block_till_done()
    assert _sns(api) == [AUTO]
    await _poll(hass, coord)
    assert rec.result == "confirmed" and rec.retries == 1


async def test_off_while_already_off_without_memory_remembers_speed(hass):
    entry, coord, api = await _setup(hass)
    _digital(api)["HVAC AC"]["current_status"] = "OFF"  # off at the panel with fan low
    _set_fan_at_panel(api, "HVAC Fan Low")
    await _poll(hass, coord)
    await _svc(hass, "turn_off")
    assert _sns(api) == [AUTO]  # power already off; fan -> auto
    assert coord.drift.prev_fan[KEY] == "low"
    await _poll(hass, coord)
    api.commands.clear()
    await _svc(hass, "turn_on")
    assert _sns(api) == [POWER, LOW]
