"""Config flow for Msheireb Smart Home."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode

from .api import MsheirebApi, MsheirebAuthError, MsheirebError
from .const import (
    CONF_CONTRACTS,
    slim_contracts,
    CONF_ADOPT_EXTERNAL,
    CONF_DRIFT_GRACE,
    CONF_EXTERNAL_CHANGE,
    DEFAULT_ADOPT_EXTERNAL,
    DEFAULT_DRIFT_GRACE,
    DEFAULT_EXTERNAL_CHANGE,
    EXT_MODES,
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_MAX_RETRIES,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_NOTIFICATIONS,
    CONF_PULSE_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEFAULT_NOTIFICATIONS,
    DEFAULT_PULSE_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_RETRIES_LIMIT,
    PULSE_INTERVAL_MAX,
    PULSE_INTERVAL_MIN,
)

_LOGGER = logging.getLogger(__name__)


async def _validate(hass: HomeAssistant, email: str, password: str) -> dict[str, Any]:
    api = MsheirebApi(async_get_clientsession(hass), email, password)
    payload = await api.async_login()
    user = payload.get("user") or {}
    return {
        "unique_id": str(user.get("id") or email.lower()),
        "data": {
            CONF_EMAIL: email,
            CONF_PASSWORD: password,
            CONF_ACCESS_TOKEN: api.access_token,
            CONF_REFRESH_TOKEN: api.refresh_token,
            CONF_EXPIRES_AT: api.expires_at,
            CONF_CONTRACTS: slim_contracts(api.contracts),
        },
        "contracts": len(api.contracts),
    }


class MsheirebConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            try:
                info = await _validate(self.hass, email, user_input[CONF_PASSWORD])
            except MsheirebAuthError:
                errors["base"] = "invalid_auth"
            except MsheirebError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"Msheireb ({email})", data=info["data"])
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input.get(CONF_EMAIL, entry.data[CONF_EMAIL]).strip()
            try:
                info = await _validate(self.hass, email, user_input[CONF_PASSWORD])
            except MsheirebAuthError:
                errors["base"] = "invalid_auth"
            except MsheirebError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data_updates=info["data"])
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=entry.data.get(CONF_EMAIL, "")): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MsheirebOptionsFlow()


class MsheirebOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_MIN_TEMP] >= user_input[CONF_MAX_TEMP]:
                errors["base"] = "min_ge_max"
            else:
                return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_MIN_TEMP, default=opts.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP)): vol.All(
                    vol.Coerce(float), vol.Range(min=5, max=40)
                ),
                vol.Required(CONF_MAX_TEMP, default=opts.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP)): vol.All(
                    vol.Coerce(float), vol.Range(min=5, max=40)
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): vol.All(vol.Coerce(int), vol.Range(min=15, max=600)),
                vol.Required(
                    CONF_PULSE_INTERVAL, default=opts.get(CONF_PULSE_INTERVAL, DEFAULT_PULSE_INTERVAL)
                ): vol.All(vol.Coerce(float), vol.Range(min=PULSE_INTERVAL_MIN, max=PULSE_INTERVAL_MAX)),
                vol.Required(
                    CONF_MAX_RETRIES, default=opts.get(CONF_MAX_RETRIES, DEFAULT_MAX_RETRIES)
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_RETRIES_LIMIT)),
                vol.Required(
                    CONF_EXTERNAL_CHANGE, default=opts.get(CONF_EXTERNAL_CHANGE, DEFAULT_EXTERNAL_CHANGE)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=EXT_MODES, translation_key="external_change", mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Required(
                    CONF_DRIFT_GRACE, default=opts.get(CONF_DRIFT_GRACE, DEFAULT_DRIFT_GRACE)
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                vol.Required(
                    CONF_ADOPT_EXTERNAL, default=opts.get(CONF_ADOPT_EXTERNAL, DEFAULT_ADOPT_EXTERNAL)
                ): bool,
                vol.Required(
                    CONF_NOTIFICATIONS, default=opts.get(CONF_NOTIFICATIONS, DEFAULT_NOTIFICATIONS)
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
