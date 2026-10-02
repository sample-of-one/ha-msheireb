"""Door unlock button: mocks only. The real unlock endpoint is NEVER called."""
import json
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.msheireb.api import MsheirebApi, MsheirebError
from custom_components.msheireb.const import API_BASE, DOMAIN

from .fake_api import FakeApi

DATA = {"email": "me@example.com", "password": "pw", "access_token": "A", "refresh_token": "R", "expires_at": 9e9}
BUTTON = "button.msheireb_demo01_unlock_door"
DOOR = "sensor.msheireb_demo01_door"


@pytest.fixture(autouse=True)
def fake_api(smart_home_payload):
    FakeApi.instances.clear()
    FakeApi.payload = smart_home_payload
    FakeApi.controller = "connected"
    FakeApi.fail_login = FakeApi.fail_fetch = None
    FakeApi.unlock_error = None
    FakeApi.unlock_calls = []
    FakeApi.lock_reads = 0
    with patch("custom_components.msheireb.MsheirebApi", FakeApi):
        yield


async def _setup(hass, options=None, enable_entity=True):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="1263", entry_id="e1", options=options or {})
    entry.add_to_hass(hass)
    if enable_entity:  # the button is disabled by default; pre-register it enabled
        er.async_get(hass).async_get_or_create("button", DOMAIN, "4242_door_unlock", config_entry=entry,
                                               suggested_object_id="msheireb_demo01_unlock_door",
                                               disabled_by=None)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data


async def _press(hass):
    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    await hass.async_block_till_done()


async def test_button_disabled_by_default(hass):
    await _setup(hass, enable_entity=False)
    reg = er.async_get(hass).async_get_entity_id("button", DOMAIN, "4242_door_unlock")
    entity = er.async_get(hass).async_get(reg)
    assert entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(reg) is None
    assert FakeApi.unlock_calls == []


async def test_press_refused_unless_option_enabled(hass):
    await _setup(hass)
    s = hass.states.get(BUTTON)
    assert s is not None and s.attributes["icon"] == "mdi:lock-open-variant"
    with pytest.raises(HomeAssistantError, match="Enable door unlock'"):
        await _press(hass)
    assert FakeApi.unlock_calls == []
    assert hass.states.get(DOOR).state == "locked"
    assert hass.states.get(DOOR).attributes["last_result"] is None


async def test_press_unlocks_logs_and_refreshes(hass, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="custom_components.msheireb")
    entry, coord = await _setup(hass, {"unlock_enabled": True})
    reads = FakeApi.lock_reads
    await _press(hass)
    assert FakeApi.unlock_calls == [(4242, "5s")]
    assert FakeApi.lock_reads > reads  # lock status refreshed after the unlock
    s = hass.states.get(DOOR)
    assert s.attributes["last_result"] == "success" and s.attributes["unlock_duration"] == "5s"
    assert s.attributes["last_unlock_at"]
    msgs = [r.getMessage() for r in caplog.records if r.name in ("custom_components.msheireb.door", "custom_components.msheireb.button")]
    assert any("Door unlock requested" in m for m in msgs) and any("accepted" in m for m in msgs)
    assert not any("4242" in m or "DEMO01" in m for m in msgs)  # no IDs in the log


async def test_portal_error_raised_with_message(hass):
    await _setup(hass, {"unlock_enabled": True})
    FakeApi.unlock_error = MsheirebError("Lock is offline")
    with pytest.raises(HomeAssistantError, match="Door unlock failed: Lock is offline"):
        await _press(hass)
    s = hass.states.get(DOOR)
    assert s.attributes["last_result"] == "failed" and s.attributes["message"] == "Lock is offline"


async def test_no_button_without_a_lock(hass):
    with patch.object(FakeApi, "async_get_lock_status", lambda self, cid: _no_locks()):
        await _setup(hass, {"unlock_enabled": True}, enable_entity=False)
    assert not [s for s in hass.states.async_all("button")]
    assert hass.states.get(DOOR) is None


async def _no_locks():
    return {"contract_id": 4242, "locks": []}


async def test_option_in_flow_default_off(hass):
    entry, _ = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    key = next(k for k in result["data_schema"].schema if k == "unlock_enabled")
    assert key.default() is False


# --------------------------------------------- real client, mocked HTTP only (aioclient_mock)
async def test_api_unlock_request_matches_portal(hass, aioclient_mock):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.post(f"{API_BASE}/smart-lock/contract-access-point",
                        json={"status": "success", "message": "ok", "data": None})
    api = MsheirebApi(async_get_clientsession(hass), "me@example.com", "pw",
                      access_token="A", refresh_token="R", expires_at=9e9)
    await api.async_unlock_door(4242)
    method, url, body, headers = aioclient_mock.mock_calls[-1]
    assert method == "POST" and str(url).endswith("/smart-lock/contract-access-point")
    assert body == {"contract_id": 4242, "state": "unlock", "duration": "5s"}
    assert headers["Authorization"] == "Bearer A"


async def test_api_unlock_error_message(hass, aioclient_mock):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.post(f"{API_BASE}/smart-lock/contract-access-point", status=400,
                        json={"status": "error", "message": "Access point not reachable"})
    api = MsheirebApi(async_get_clientsession(hass), "me@example.com", "pw",
                      access_token="A", refresh_token="R", expires_at=9e9)
    with pytest.raises(MsheirebError, match="Access point not reachable"):
        await api.async_unlock_door(4242)




# --------------------------------------------- Door sensor: locked -> unlocking -> open -> locked
from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: E402


def _events(hass):
    seen = []
    hass.bus.async_listen("msheireb_door_unlocked", lambda e: seen.append(dict(e.data)))
    return seen


def _door_states(hass):
    seen = []

    def _cb(e):
        if e.data["entity_id"] == DOOR and e.data["new_state"] is not None:
            item = (e.data["new_state"].state, e.data["new_state"].attributes.get("icon"))
            if not seen or seen[-1] != item:  # attribute-only updates repeat the state
                seen.append(item)

    hass.bus.async_listen("state_changed", _cb)
    return seen


async def _tick(hass, freezer, seconds):
    freezer.tick(seconds)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_door_sensor_basics(hass):
    await _setup(hass)
    s = hass.states.get(DOOR)
    assert s.state == "locked" and s.attributes["icon"] == "mdi:lock"
    assert s.attributes["options"] == ["locked", "unlocking", "unlocked", "failed"]
    assert s.attributes["device_class"] == "enum"
    assert s.attributes["lock_connected"] is True and s.attributes["low_battery"] is False
    assert not hass.states.async_all("lock")  # no lock entity (the portal cannot lock)


async def test_fast_unlock_shows_unlocking_for_2s_then_open_5s(hass, freezer):
    entry, coord = await _setup(hass, {"unlock_enabled": True})
    events = _events(hass)
    seen = _door_states(hass)
    await _press(hass)  # the mocked portal answers instantly
    assert hass.states.get(DOOR).state == "unlocking"  # still shown: minimum ~2 s
    assert events and events[-1]["result"] == "success" and events[-1]["source"] == "button"
    with pytest.raises(HomeAssistantError, match="already in progress"):
        await _press(hass)
    await _tick(hass, freezer, 1.0)
    assert hass.states.get(DOOR).state == "unlocking"
    await _tick(hass, freezer, 1.1)
    assert hass.states.get(DOOR).state == "unlocked"
    await coord.async_refresh()  # a poll during the open window does not overwrite it
    await hass.async_block_till_done()
    assert hass.states.get(DOOR).state == "unlocked"
    await _tick(hass, freezer, 5.0)  # (the test timer helper fires up to 0.5 s early)
    assert hass.states.get(DOOR).state == "unlocked"  # unlocked for the full 6 s
    await _tick(hass, freezer, 1.1)
    assert hass.states.get(DOOR).state == "locked"
    assert [st for st, _i in seen] == ["unlocking", "unlocked", "locked"]
    assert [i for _s, i in seen] == ["mdi:lock-clock", "mdi:lock-open-variant", "mdi:lock"]
    assert FakeApi.unlock_calls == [(4242, "5s")]


async def test_unlocking_written_before_the_request_and_held_while_pending(hass, freezer):
    """The state is written on press (before the POST is awaited) and lasts while POST + status re-read run."""
    import asyncio

    entry, coord = await _setup(hass, {"unlock_enabled": True})
    post_started, release_post = asyncio.Event(), asyncio.Event()
    status_started, release_status = asyncio.Event(), asyncio.Event()
    orig_status = FakeApi.async_get_lock_status

    async def slow_unlock(self, cid, duration="5s"):
        FakeApi.unlock_calls.append((cid, duration))
        assert hass.states.get(DOOR).state == "unlocking"  # already written when the POST starts
        post_started.set()
        await release_post.wait()

    async def slow_status(self, cid):
        if FakeApi.unlock_calls:
            status_started.set()
            await release_status.wait()
        return await orig_status(self, cid)

    with patch.object(FakeApi, "async_unlock_door", slow_unlock), \
         patch.object(FakeApi, "async_get_lock_status", slow_status):
        task = asyncio.ensure_future(hass.services.async_call(
            "button", "press", {"entity_id": BUTTON}, blocking=True))
        await post_started.wait()
        freezer.tick(3)  # slower than the 2 s minimum (no block_till_done: the press task is held)
        async_fire_time_changed(hass)
        await asyncio.sleep(0)
        assert hass.states.get(DOOR).state == "unlocking"
        release_post.set()
        await status_started.wait()
        assert hass.states.get(DOOR).state == "unlocking"  # status re-read still pending
        release_status.set()
        await task
    await hass.async_block_till_done()
    assert hass.states.get(DOOR).state == "unlocking"  # > 2 s already, but the lock needs 1.5 s more
    await _tick(hass, freezer, 0.9)  # (the test timer helper fires up to 0.5 s early)
    assert hass.states.get(DOOR).state == "unlocking"
    await _tick(hass, freezer, 0.7)
    assert hass.states.get(DOOR).state == "unlocked"
    await _tick(hass, freezer, 5.0)
    assert hass.states.get(DOOR).state == "unlocked"
    await _tick(hass, freezer, 1.1)
    assert hass.states.get(DOOR).state == "locked"


async def test_failure_shows_failed_for_10s_with_message(hass, freezer):
    await _setup(hass, {"unlock_enabled": True})
    events = _events(hass)
    seen = _door_states(hass)
    FakeApi.unlock_error = MsheirebError("Lock is offline")
    with pytest.raises(HomeAssistantError, match="Door unlock failed: Lock is offline"):
        await _press(hass)
    await hass.async_block_till_done()
    assert hass.states.get(DOOR).state == "unlocking"  # minimum display first
    await _tick(hass, freezer, 2.1)
    s = hass.states.get(DOOR)
    assert s.state == "failed" and s.attributes["icon"] == "mdi:lock-alert"
    assert s.attributes["message"] == "Lock is offline" and s.attributes["problem"] == "Lock is offline"
    assert events[-1]["result"] == "failed" and events[-1]["message"] == "Lock is offline"
    await _tick(hass, freezer, 9.0)
    assert hass.states.get(DOOR).state == "failed"
    await _tick(hass, freezer, 1.2)
    assert hass.states.get(DOOR).state == "locked"
    assert [st for st, _i in seen] == ["unlocking", "failed", "locked"]
    # the next successful unlock clears the problem
    FakeApi.unlock_error = None
    await _press(hass)
    await _tick(hass, freezer, 2.1)
    s = hass.states.get(DOOR)
    assert s.state == "unlocked" and s.attributes["problem"] is None and s.attributes["last_result"] == "success"
    await _tick(hass, freezer, 6.6)


async def test_retired_lock_and_last_unlock_entities_removed(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="1263", entry_id="e1", options={})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    reg.async_get_or_create("lock", DOMAIN, "4242_door_lock", config_entry=entry,
                            suggested_object_id="msheireb_demo01_door")
    reg.async_get_or_create("sensor", DOMAIN, "e1_last_unlock_4242", config_entry=entry,
                            suggested_object_id="msheireb_demo01_last_unlock")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get_entity_id("lock", DOMAIN, "4242_door_lock") is None
    assert reg.async_get_entity_id("sensor", DOMAIN, "e1_last_unlock_4242") is None
    assert reg.async_get_entity_id("sensor", DOMAIN, "4242_door") == DOOR
    assert hass.states.get(DOOR).state == "locked"


def test_duration_parsing():
    from custom_components.msheireb.door import duration_seconds

    assert duration_seconds("5s") == 5 and duration_seconds("500ms") == 0.5 and duration_seconds("1m") == 60
    assert duration_seconds("bogus") == 5
