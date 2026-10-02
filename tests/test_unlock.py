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
LAST = "sensor.msheireb_demo01_last_unlock"


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
    if enable_entity:  # button + lock are disabled by default; pre-register them enabled
        er.async_get(hass).async_get_or_create("button", DOMAIN, "4242_door_unlock", config_entry=entry,
                                               suggested_object_id="msheireb_demo01_unlock_door",
                                               disabled_by=None)
        er.async_get(hass).async_get_or_create("lock", DOMAIN, "4242_door_lock", config_entry=entry,
                                               suggested_object_id="msheireb_demo01_door",
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
    assert hass.states.get(LAST).state == "unknown"


async def test_press_unlocks_logs_and_refreshes(hass, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="custom_components.msheireb")
    entry, coord = await _setup(hass, {"unlock_enabled": True})
    reads = FakeApi.lock_reads
    await _press(hass)
    assert FakeApi.unlock_calls == [(4242, "5s")]
    assert FakeApi.lock_reads > reads  # lock status refreshed after the unlock
    s = hass.states.get(LAST)
    assert s.state == "success" and s.attributes["duration"] == "5s" and s.attributes["at"]
    msgs = [r.getMessage() for r in caplog.records if r.name in ("custom_components.msheireb.door", "custom_components.msheireb.button")]
    assert any("Door unlock requested" in m for m in msgs) and any("accepted" in m for m in msgs)
    assert not any("4242" in m or "DEMO01" in m for m in msgs)  # no IDs in the log


async def test_portal_error_raised_with_message(hass):
    await _setup(hass, {"unlock_enabled": True})
    FakeApi.unlock_error = MsheirebError("Lock is offline")
    with pytest.raises(HomeAssistantError, match="Door unlock failed: Lock is offline"):
        await _press(hass)
    s = hass.states.get(LAST)
    assert s.state == "failed" and s.attributes["message"] == "Lock is offline"


async def test_no_button_without_a_lock(hass):
    with patch.object(FakeApi, "async_get_lock_status", lambda self, cid: _no_locks()):
        await _setup(hass, {"unlock_enabled": True}, enable_entity=False)
    assert not [s for s in hass.states.async_all("button")]


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


# --------------------------------------------- Door lock entity: locked -> unlocking -> open -> locked
LOCK = "lock.msheireb_demo01_door"


async def _lock_service(hass, service):
    await hass.services.async_call("lock", service, {"entity_id": LOCK}, blocking=True)


def _events(hass):
    seen = []
    hass.bus.async_listen("msheireb_door_unlocked", lambda e: seen.append(dict(e.data)))
    return seen


async def test_lock_disabled_by_default(hass):
    await _setup(hass, enable_entity=False)
    reg = er.async_get(hass)
    entity = reg.async_get(reg.async_get_entity_id("lock", DOMAIN, "4242_door_lock"))
    assert entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(LOCK) is None


async def test_lock_unlock_refused_without_option(hass):
    await _setup(hass)
    assert hass.states.get(LOCK).state == "locked"
    for service in ("unlock", "open"):
        with pytest.raises(HomeAssistantError, match="Enable door unlock'"):
            await _lock_service(hass, service)
    assert FakeApi.unlock_calls == [] and hass.states.get(LOCK).state == "locked"


async def test_lock_states_follow_the_portal_flow(hass, freezer):
    """unlocking while the POST + status re-read run (the portal spinner), then open for 5 s."""
    import asyncio

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry, coord = await _setup(hass, {"unlock_enabled": True})
    events = _events(hass)
    seen = []
    hass.bus.async_listen("state_changed",
                          lambda e: e.data["entity_id"] == LOCK and seen.append(e.data["new_state"].state))
    post_started, release_post = asyncio.Event(), asyncio.Event()
    status_started, release_status = asyncio.Event(), asyncio.Event()
    orig_status = FakeApi.async_get_lock_status

    async def slow_unlock(self, cid, duration="5s"):
        FakeApi.unlock_calls.append((cid, duration))
        post_started.set()
        await release_post.wait()

    async def slow_status(self, cid):
        if FakeApi.unlock_calls:
            status_started.set()
            await release_status.wait()
        return await orig_status(self, cid)

    with patch.object(FakeApi, "async_unlock_door", slow_unlock), \
         patch.object(FakeApi, "async_get_lock_status", slow_status):
        task = hass.async_create_task(_lock_service(hass, "open"))
        await post_started.wait()
        assert hass.states.get(LOCK).state == "unlocking"  # spinner: POST pending
        with pytest.raises(HomeAssistantError, match="already in progress"):
            await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
        release_post.set()
        await status_started.wait()
        assert hass.states.get(LOCK).state == "unlocking"  # spinner: status re-read pending
        release_status.set()
        await task
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == "open"
    assert events == [{"contract_id": 4242, "entity_id": LOCK, "result": "success", "duration": "5s",
                       "message": None, "source": "lock"}]
    freezer.tick(4)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == "open"
    freezer.tick(1.5)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == "locked"
    assert seen == ["unlocking", "open", "locked"]
    assert FakeApi.unlock_calls == [(4242, "5s")]
    assert hass.states.get(LAST).state == "success"


async def test_lock_failure_sets_problem_and_event(hass):
    await _setup(hass, {"unlock_enabled": True})
    events = _events(hass)
    FakeApi.unlock_error = MsheirebError("Lock is offline")
    with pytest.raises(HomeAssistantError, match="Door unlock failed: Lock is offline"):
        await _lock_service(hass, "unlock")
    await hass.async_block_till_done()
    s = hass.states.get(LOCK)
    assert s.state == "locked" and s.attributes["problem"] == "Lock is offline"
    assert s.attributes["last_result"] == "failed"
    assert events[-1]["result"] == "failed" and events[-1]["message"] == "Lock is offline"
    assert hass.states.get(LAST).state == "failed"
    # the next successful unlock clears the problem
    FakeApi.unlock_error = None
    await _lock_service(hass, "unlock")
    s = hass.states.get(LOCK)
    assert s.state == "open" and s.attributes["problem"] is None


async def test_button_drives_the_lock_state(hass):
    await _setup(hass, {"unlock_enabled": True})
    events = _events(hass)
    await _press(hass)
    assert hass.states.get(LOCK).state == "open"
    assert events[-1]["source"] == "button" and events[-1]["result"] == "success"


async def test_locking_not_supported(hass):
    await _setup(hass, {"unlock_enabled": True})
    with pytest.raises(HomeAssistantError, match="Locking is not supported"):
        await _lock_service(hass, "lock")
    assert FakeApi.unlock_calls == []


async def test_lock_attributes_from_status(hass):
    await _setup(hass)
    a = hass.states.get(LOCK).attributes
    assert a["lock_connected"] is True and a["low_battery"] is False and a["unlock_duration"] == "5s"
    assert a["supported_features"] == 1  # OPEN


def test_duration_parsing():
    from custom_components.msheireb.door import duration_seconds

    assert duration_seconds("5s") == 5 and duration_seconds("500ms") == 0.5 and duration_seconds("1m") == 60
    assert duration_seconds("bogus") == 5
