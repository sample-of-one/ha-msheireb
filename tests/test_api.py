"""Token handling against mocked HTTP (no real network)."""
import time

import pytest

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.msheireb.api import MsheirebApi, MsheirebAuthError
from custom_components.msheireb.const import API_BASE

LOGIN = {"status": "success", "data": {"access_token": "A1", "refresh_token": "R1",
         "token_type": "Bearer", "expires_in": 86400, "user": {"id": 5}, "contracts": [{"id": 4242}]}}
REFRESHED = {"status": "success", "data": {"access_token": "A2", "refresh_token": "R2",
             "token_type": "Bearer", "user": {"id": 5}, "contracts": [{"id": 4242}]}}


async def test_refresh_on_401_then_persist(hass, aioclient_mock, smart_home_payload):
    saved = []
    api = MsheirebApi(async_get_clientsession(hass), "e", "p", "OLD", "R0",
                      time.time() + 50000, lambda a, r, e: saved.append((a, r)))
    api.contracts = [{"id": 4242}]
    aioclient_mock.get(f"{API_BASE}/user/contracts/4242/smart-home", status=401,
                       json={"status": "error", "message": "Unauthenticated."})
    aioclient_mock.post(f"{API_BASE}/user/refresh-token", json=REFRESHED)
    with pytest.raises(MsheirebAuthError):
        # mock always returns 401 -> after one renewal it must give up, not loop
        await api.async_get_smart_home(4242)
    assert saved == [("A2", "R2")]
    assert api.refresh_token == "R2"


async def test_refresh_rejected_falls_back_to_login(hass, aioclient_mock, smart_home_payload):
    saved = []
    api = MsheirebApi(async_get_clientsession(hass), "e", "p", "OLD", "R0",
                      time.time() - 10, lambda a, r, e: saved.append((a, r)))
    aioclient_mock.post(f"{API_BASE}/user/refresh-token", status=401, json={"message": "invalid"})
    aioclient_mock.post(f"{API_BASE}/user/login", json=LOGIN)
    aioclient_mock.get(f"{API_BASE}/user/contracts/4242/smart-home",
                       json={"status": "success", "data": smart_home_payload})
    data = await api.async_get_smart_home(4242)
    assert data["apartment_ip"] == "10.0.0.10"
    assert saved[-1] == ("A1", "R1")
    # login body must be exactly {email, password}
    login_call = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/user/login")][0]
    assert login_call[2] == {"email": "e", "password": "p"}


async def test_bad_login_is_auth_error(hass, aioclient_mock):
    api = MsheirebApi(async_get_clientsession(hass), "e", "bad")
    aioclient_mock.post(f"{API_BASE}/user/login", status=401, json={"message": "Invalid credentials"})
    with pytest.raises(MsheirebAuthError):
        await api.async_login()


async def test_auth_status_transitions_and_timing(hass, aioclient_mock, smart_home_payload):
    statuses = []
    api = MsheirebApi(async_get_clientsession(hass), "e", "p", "OLD", "R0", time.time() - 10,
                      None, statuses.append)
    aioclient_mock.post(f"{API_BASE}/user/refresh-token", status=401, json={"message": "invalid"})
    aioclient_mock.post(f"{API_BASE}/user/login", json=LOGIN)
    aioclient_mock.get(f"{API_BASE}/user/contracts/4242/smart-home",
                       json={"status": "success", "data": smart_home_payload})
    await api.async_get_smart_home(4242)
    assert statuses == ["refreshing", "relogin", "ok"]
    assert api.last_response_ms is not None


async def test_404_is_not_found(hass, aioclient_mock):
    from custom_components.msheireb.api import MsheirebNotFoundError
    api = MsheirebApi(async_get_clientsession(hass), "e", "p", "A", "R", time.time() + 90000)
    api.contracts = [{"id": 1}]
    aioclient_mock.get(f"{API_BASE}/user/contracts/1/smart-home", status=404, json={"message": "nope"})
    with pytest.raises(MsheirebNotFoundError):
        await api.async_get_smart_home(1)


async def test_login_failure_sets_failed(hass, aioclient_mock):
    statuses = []
    api = MsheirebApi(async_get_clientsession(hass), "e", "p", None, None, 0, None, statuses.append)
    aioclient_mock.post(f"{API_BASE}/user/login", status=401, json={"message": "bad"})
    with pytest.raises(MsheirebAuthError):
        await api.async_get_contracts()
    assert statuses[-1] == "failed"
