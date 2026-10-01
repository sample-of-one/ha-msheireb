"""v0.3.11: the climate card reacts instantly; presses run in the background (latest action wins)."""
import asyncio
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.persistent_notification import _async_get_or_create_notifications

import custom_components.msheireb.climate as cl
from custom_components.msheireb.const import DOMAIN

from .fake_api import FakeApi
from .test_power_fan_sequence import DINING, KEY, LAST, _advance, _setup, _state, ac  # noqa: F401

ENTRY_DATA = {"email": "me@example.com", "password": "pw", "access_token": "A",
              "refresh_token": "R", "expires_at": 9e9}


def _record_states(hass):
    seen = []

    def _cb(event):
        if event.data["entity_id"] == DINING and event.data.get("new_state") is not None:
            s = event.data["new_state"]
            seen.append((s.state, s.attributes.get("fan_mode"), s.attributes.get("temperature")))

    hass.bus.async_listen("state_changed", _cb)
    return seen


async def _call(hass, service, **data):
    await hass.services.async_call("climate", service, {"entity_id": DINING, **data}, blocking=True)


async def _until(cond, limit=500):
    for _ in range(limit):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


def _command_notes(hass):
    return [k for k in _async_get_or_create_notifications(hass) if k.startswith(DOMAIN) and "command_" in k]


# ------------------------------------------------ realistic AC (production timing, frozen clock)
@pytest.mark.real_sequencing
@pytest.mark.background_sequences
async def test_immediate_state_then_background_sequence(hass, ac, freezer):
    entry, coord = await _setup(hass)
    coord.drift.prev_fan[KEY] = "high"
    await _call(hass, "turn_on")
    # the service returned before the sequence ran; the card already shows the request
    assert _state(hass) == ("cool", "high")
    assert "high" not in ac.presses and coord.room_busy(KEY)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ac.presses == ["power", "high"]
    await _advance(hass, freezer, 20)
    assert hass.states.get(LAST).state == "confirmed"
    assert _state(hass) == ("cool", "high") and not coord.room_busy(KEY)


@pytest.mark.real_sequencing
@pytest.mark.background_sequences
async def test_no_flicker_and_no_drift_while_settling(hass, ac, freezer):
    entry, coord = await _setup(hass, {"external_change": "restore_notify", "drift_grace": 0})
    coord.drift.prev_fan[KEY] = "high"
    coord.drift.set_desired(KEY, {"power": False})
    ac.power_latency = 30.0  # slow unit: many polls still report OFF after the press
    seen = _record_states(hass)
    await _call(hass, "turn_on")
    contradicting = 0
    for _ in range(300):
        if "power" in ac.presses and not ac.power:
            contradicting += 1
        await coord.async_refresh()  # regular polls during the settle/power wait
        if "high" in ac.presses:
            break
        await asyncio.sleep(0)
    assert contradicting >= 2  # polls really reported the old state
    await hass.async_block_till_done(wait_background_tasks=True)
    await _advance(hass, freezer, 30)
    assert hass.states.get(LAST).state == "confirmed"
    assert {(s, f) for s, f, _t in seen} == {("cool", "high")}  # never flickered back to off/auto
    assert coord.drift.events == 0  # no external change reported for our own settling


@pytest.mark.real_sequencing
@pytest.mark.background_sequences
async def test_revert_only_after_final_retry_fails(hass, ac, freezer):
    ac.power, ac.fan = True, "low"
    entry, coord = await _setup(hass, {"max_retries": 1})
    ac.ignore_fan = True  # the AC never takes the fan press
    seen = _record_states(hass)
    await _call(hass, "set_fan_mode", fan_mode="high")
    assert _state(hass) == ("cool", "high")
    await hass.async_block_till_done(wait_background_tasks=True)
    for _ in range(30):  # polls + retry while in flight: the card keeps the request
        await _advance(hass, freezer, 2)
        await coord.async_refresh()
        rec = coord.health.commands[KEY]
        if rec.result != "pending":
            break
        assert _state(hass) == ("cool", "high")
    await _advance(hass, freezer, 120, step=2)
    rec = coord.health.commands[KEY]
    assert rec.result == "not_confirmed" and rec.retries == 1
    assert ac.presses == ["high", "high"]
    fans = [f for _s, f, _t in seen]
    first_low = fans.index("low")
    assert set(fans[:first_low]) == {"high"} and set(fans[first_low:]) == {"low"}
    assert _state(hass) == ("cool", "low")  # actual state after the final failure
    assert _command_notes(hass)


@pytest.mark.real_sequencing
@pytest.mark.background_sequences
async def test_newer_action_supersedes_running_sequence(hass, ac, freezer):
    entry, coord = await _setup(hass)
    coord.drift.prev_fan[KEY] = "high"
    await _call(hass, "turn_on")
    await _until(lambda: ac.presses == ["power"])  # turn-on sequence is settling
    await _call(hass, "turn_off")
    assert _state(hass)[0] == "off"  # latest request shown at once
    await hass.async_block_till_done(wait_background_tasks=True)
    await _advance(hass, freezer, 40)
    assert "high" not in ac.presses  # the superseded turn-on stopped before its fan press
    assert ac.presses.count("power") == 2 and not ac.power
    assert hass.states.get(LAST).state == "confirmed"
    assert _state(hass) == ("off", "auto")
    assert not _command_notes(hass)


# ------------------------------------------------ simple fake controller (instant)
@pytest.fixture
def fake(smart_home_payload):
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


async def _setup_fake(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="1263")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data, FakeApi.instances[-1]


@pytest.mark.background_sequences
async def test_temperature_taps_are_debounced_into_one_target(hass, fake):
    coord, api = await _setup_fake(hass)
    with patch.object(cl, "TEMP_DEBOUNCE", 0.3):
        for target in (20.0, 20.5, 21.0, 21.5):  # four quick + taps from 19.5
            await _call(hass, "set_temperature", temperature=target)
            assert hass.states.get(DINING).attributes["temperature"] == target  # instant
            await asyncio.sleep(0.1)
        assert api.commands == []  # nothing pressed while tapping
        await hass.async_block_till_done(wait_background_tasks=True)
    assert [c["sn"] for c in api.commands] == [6, 6, 6, 6]  # one run 19.5 -> 21.5
    rec = coord.health.commands[KEY]
    assert rec.expected == {"target": 21.5} and len(rec.pulses) == 4
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert rec.result == "confirmed"
    assert hass.states.get(DINING).attributes["temperature"] == 21.5


@pytest.mark.background_sequences
async def test_newer_fan_choice_supersedes_pending_command(hass, fake):
    coord, api = await _setup_fake(hass)
    FakeApi.ignore_commands = True  # first choice is pressed but never shows up
    await _call(hass, "set_fan_mode", fan_mode="low")
    await hass.async_block_till_done(wait_background_tasks=True)
    first = coord.health.commands[KEY]
    assert first.result == "pending"
    FakeApi.ignore_commands = False
    await _call(hass, "set_fan_mode", fan_mode="medium")
    assert first.result == "superseded"
    assert hass.states.get(DINING).attributes["fan_mode"] == "medium"
    await hass.async_block_till_done(wait_background_tasks=True)
    await coord.async_refresh()
    await hass.async_block_till_done()
    rec = coord.health.commands[KEY]
    assert rec is not first and rec.result == "confirmed"
    assert coord.health.commands_failed == 0 and not _command_notes(hass)
    assert hass.states.get(DINING).attributes["fan_mode"] == "medium"
