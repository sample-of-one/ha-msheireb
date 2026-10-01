"""Persistent notifications + repair issues for Msheireb."""
from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_NOTIFICATIONS, DEFAULT_NOTIFICATIONS, DOMAIN


class Alerts:
    """Creates/dismisses notifications idempotently; respects the options toggle."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.active: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.entry.options.get(CONF_NOTIFICATIONS, DEFAULT_NOTIFICATIONS))

    def _nid(self, key: str) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_{key}"

    def raise_(self, key: str, title: str, message: str) -> None:
        if not self.enabled:
            return
        persistent_notification.async_create(self.hass, message, title=title, notification_id=self._nid(key))
        self.active.add(key)

    def clear(self, key: str) -> None:
        if key in self.active:
            persistent_notification.async_dismiss(self.hass, self._nid(key))
            self.active.discard(key)

    def clear_all(self) -> None:
        for key in list(self.active):
            self.clear(key)

    # repair issue for reauth
    @property
    def issue_id(self) -> str:
        return f"reauth_{self.entry.entry_id}"

    def raise_reauth_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="reauth_required",
            translation_placeholders={"title": self.entry.title},
        )

    def clear_reauth_issue(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
