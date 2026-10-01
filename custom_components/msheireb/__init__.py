"""Msheireb Smart Home integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import MsheirebApi
from .const import CONF_ACCESS_TOKEN, CONF_EXPIRES_AT, CONF_REFRESH_TOKEN, DOMAIN, STORE_VERSION
from .drift import store_key
from .coordinator import MsheirebCoordinator

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]

type MsheirebConfigEntry = ConfigEntry[MsheirebCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MsheirebConfigEntry) -> bool:
    @callback
    def _persist_tokens(access: str, refresh: str, expires_at: float) -> None:
        # Persist the rotated refresh token so a restart keeps the session.
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: access,
                CONF_REFRESH_TOKEN: refresh,
                CONF_EXPIRES_AT: expires_at,
            },
        )

    api = MsheirebApi(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        expires_at=entry.data.get(CONF_EXPIRES_AT),
        token_callback=_persist_tokens,
        status_callback=lambda status: coordinator.on_auth_status(status),
    )
    coordinator = MsheirebCoordinator(hass, entry, api)
    await coordinator.drift.async_load()  # desired state survives restarts
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_cancel_pending)

    options_snapshot = dict(entry.options)

    async def _options_updated(hass: HomeAssistant, updated: ConfigEntry) -> None:
        # Token persistence also triggers update listeners; reload only on option changes.
        if dict(updated.options) != options_snapshot:
            await hass.config_entries.async_reload(updated.entry_id)

    entry.async_on_unload(entry.add_update_listener(_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MsheirebConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and getattr(entry, "runtime_data", None) is not None:
        # notifications belong to this run; the reauth repair issue is kept until fixed
        entry.runtime_data.alerts.clear_all()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: MsheirebConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, f"reauth_{entry.entry_id}")
    await Store(hass, STORE_VERSION, store_key(entry.entry_id)).async_remove()
