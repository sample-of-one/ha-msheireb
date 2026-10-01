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
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

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
    CONF_POWER_FAN_DELAY,
    DEFAULT_POWER_FAN_DELAY,
    CONF_FAN_AUTO_WHEN_OFF,
    DEFAULT_FAN_AUTO_WHEN_OFF,
    POWER_FAN_DELAY_MAX,
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
                {vol.Required(CONF_EMAIL): EMAIL_SELECTOR, vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}
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
                    vol.Required(CONF_EMAIL, default=entry.data.get(CONF_EMAIL, "")): EMAIL_SELECTOR,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MsheirebOptionsFlow()


TEMP_MIN_LIMIT = 10.0
TEMP_MAX_LIMIT = 35.0
SCAN_MIN, SCAN_MAX = 15, 600
GRACE_MAX = 3600
INT_OPTIONS = (CONF_SCAN_INTERVAL, CONF_MAX_RETRIES, CONF_DRIFT_GRACE, CONF_POWER_FAN_DELAY)
FLOAT_OPTIONS = (CONF_MIN_TEMP, CONF_MAX_TEMP, CONF_PULSE_INTERVAL)

EMAIL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))
PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))


def _num(min_: float, max_: float, step: float, unit: str | None = None,
         mode: NumberSelectorMode = NumberSelectorMode.BOX) -> NumberSelector:
    cfg = NumberSelectorConfig(min=min_, max=max_, step=step, mode=mode)
    if unit:
        cfg["unit_of_measurement"] = unit
    return NumberSelector(cfg)


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return min(max(float(value), lo), hi)
    except (TypeError, ValueError):
        return default


def normalize_options(data: dict[str, Any]) -> dict[str, Any]:
    """NumberSelector returns floats; store whole-number options as int."""
    out = dict(data)
    for key in INT_OPTIONS:
        if key in out and out[key] is not None:
            out[key] = int(round(float(out[key])))
    for key in FLOAT_OPTIONS:
        if key in out and out[key] is not None:
            out[key] = float(out[key])
    return out


class MsheirebOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = normalize_options(user_input)
            if user_input[CONF_MIN_TEMP] >= user_input[CONF_MAX_TEMP]:
                errors["base"] = "min_ge_max"
            else:
                return self.async_create_entry(data=user_input)
        opts = {**self.config_entry.options, **(user_input or {})}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MIN_TEMP,
                    default=_clamp(opts.get(CONF_MIN_TEMP), TEMP_MIN_LIMIT, TEMP_MAX_LIMIT, DEFAULT_MIN_TEMP),
                ): _num(TEMP_MIN_LIMIT, TEMP_MAX_LIMIT, 0.5, "°C"),
                vol.Required(
                    CONF_MAX_TEMP,
                    default=_clamp(opts.get(CONF_MAX_TEMP), TEMP_MIN_LIMIT, TEMP_MAX_LIMIT, DEFAULT_MAX_TEMP),
                ): _num(TEMP_MIN_LIMIT, TEMP_MAX_LIMIT, 0.5, "°C"),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=int(_clamp(opts.get(CONF_SCAN_INTERVAL), SCAN_MIN, SCAN_MAX, DEFAULT_SCAN_INTERVAL)),
                ): _num(SCAN_MIN, SCAN_MAX, 1, "s"),
                vol.Required(
                    CONF_PULSE_INTERVAL,
                    default=_clamp(opts.get(CONF_PULSE_INTERVAL), PULSE_INTERVAL_MIN, PULSE_INTERVAL_MAX,
                                   DEFAULT_PULSE_INTERVAL),
                ): _num(PULSE_INTERVAL_MIN, PULSE_INTERVAL_MAX, 0.5, "s"),
                vol.Required(
                    CONF_FAN_AUTO_WHEN_OFF,
                    default=bool(opts.get(CONF_FAN_AUTO_WHEN_OFF, DEFAULT_FAN_AUTO_WHEN_OFF)),
                ): BooleanSelector(),
                vol.Required(
                    CONF_POWER_FAN_DELAY,
                    default=int(_clamp(opts.get(CONF_POWER_FAN_DELAY), 0, POWER_FAN_DELAY_MAX,
                                       DEFAULT_POWER_FAN_DELAY)),
                ): _num(0, POWER_FAN_DELAY_MAX, 1, "s"),
                vol.Required(
                    CONF_MAX_RETRIES,
                    default=int(_clamp(opts.get(CONF_MAX_RETRIES), 0, MAX_RETRIES_LIMIT, DEFAULT_MAX_RETRIES)),
                ): _num(0, MAX_RETRIES_LIMIT, 1),
                vol.Required(
                    CONF_EXTERNAL_CHANGE, default=opts.get(CONF_EXTERNAL_CHANGE, DEFAULT_EXTERNAL_CHANGE)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=EXT_MODES, translation_key="external_change", mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Required(
                    CONF_DRIFT_GRACE,
                    default=int(_clamp(opts.get(CONF_DRIFT_GRACE), 0, GRACE_MAX, DEFAULT_DRIFT_GRACE)),
                ): _num(0, GRACE_MAX, 1, "s"),
                vol.Required(
                    CONF_ADOPT_EXTERNAL, default=bool(opts.get(CONF_ADOPT_EXTERNAL, DEFAULT_ADOPT_EXTERNAL))
                ): BooleanSelector(),
                vol.Required(
                    CONF_NOTIFICATIONS, default=bool(opts.get(CONF_NOTIFICATIONS, DEFAULT_NOTIFICATIONS))
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
