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
    with pytest.raises(HomeAssistantError, match="Enable door unlock button"):
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
    msgs = [r.getMessage() for r in caplog.records if r.name.startswith("custom_components.msheireb.button")]
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
