# Msheireb Smart Home for Home Assistant (v0.3.0)

[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration) [Releases](https://github.com/sample-of-one/ha-msheireb/releases) · [Issues](https://github.com/sample-of-one/ha-msheireb/issues)

Unofficial custom integration for the in-apartment AC (fan coil units) at **Msheireb Downtown Doha**. It works through the resident portal (`web-prod.mp-mdd.com`), using the same cloud API the portal uses (`mob-prod.mp-mdd.com:8443/api`).

- One **climate** entity per room: Off/Cool, target temperature (0.5 °C steps), current room temperature, fan Auto/Low/Medium/High.
- **Binary sensors** (diagnostic): apartment controller connected, door lock connection, door lock low battery, portal reachable.
- **Monitoring sensors** (diagnostic, on a "Msheireb portal" device): auth status (ok / refreshing / relogin / failed), last successful update, last error (with `type` attribute), API response time (ms), consecutive failures, token expiry, commands sent / confirmed / failed, command retries. There is also one **"<Room> last command"** sensor per room (pending / confirmed / not_confirmed / failed), with attributes for what was sent, the expected state, when it was sent, the confirmation window, the retry count, and how long it took to confirm.
- **Notifications** (built-in persistent notifications, can be turned off in Options). Sent when the login fails and re-authentication is needed, when the apartment controller has been offline for more than 5 min, when the portal has been unreachable for more than 5 min, or when a command is still not confirmed after the final automatic retry. They are dismissed automatically on recovery. A login failure also raises a **Repairs** issue.
- **External change detection and restore** ("drift"): for each room, the integration remembers the state you last set from Home Assistant (setpoint, power, fan) in HA storage, so it survives restarts. If a poll shows the room differs from that (for example after a power-outage reset, or a change from the wall panel or portal) while no HA command is in progress, that counts as an external change.
  - It acts only after a grace period (default 60 s) **and** at least 2 polls, to avoid flapping.
  - **On external change** option: *Restore + notify* (default), *Restore*, *Notify only*, or *Ignore*.
  - Restoring uses the same safe command path: it re-reads the state, sends only the needed presses, never toggles power blindly, and retries.
  - The notification names the room, what changed (from → to), and the action taken.
  - Each room has an **"<Room> auto-restore"** switch to turn restoring on or off quickly. It is saved across restarts.
  - Option **Adopt external changes as the new desired state** (default off): the external value becomes the new target instead of being restored.
  - An **External change events** counter sensor shows the last room, changes and action as attributes.
  - Rooms you never controlled from HA are not watched. A command that just failed is not silently repeated by the restore logic.
- **Diagnostics download** (Settings → Devices & services → Msheireb → ⋮ → Download diagnostics), with credentials, tokens, IPs, unit/contract IDs and lock names redacted.
- Supports multiple contracts on one account. Polls every 30 s (configurable).

> Not affiliated with Msheireb Properties. It uses an undocumented API that may change without notice.

## How it controls the AC
The portal's controller accepts **pulse** commands. Each *Temp Up/Down* pulse moves the setpoint by 0.5 °C (verified). To set a temperature, the integration sends the needed number of Up/Down pulses one at a time, 5.0 s apart start-to-start by default (configurable 0.5–10 s; the real AC also registered 3 presses 1.2–1.5 s apart, so you can lower it for faster changes), and re-reads the setpoint every 3 pulses. It never sends more pulses than initially needed. Power and fan pulses are only sent when the reported state differs from the requested one. Control serial numbers are discovered from the control labels, not hard-coded. The UI updates optimistically and is reconciled with the next poll, about 5 s after a command.

## Installation

### Option A: HACS (custom repository)
1. In Home Assistant: **HACS → ⋮ → Custom repositories**. Add `https://github.com/sample-of-one/ha-msheireb` with category **Integration**.
2. Search for **Msheireb Smart Home** in HACS, then **Download**.
3. Restart Home Assistant.

### Option B: manual copy (Samba share or File editor / Studio Code Server add-on)
1. Download the source zip from the [latest release](https://github.com/sample-of-one/ha-msheireb/releases/latest) and copy `custom_components/msheireb/` to `/config/custom_components/msheireb/` on your HA Green. The final path must be `/config/custom_components/msheireb/manifest.json`.
2. Restart Home Assistant.

### Configure
**Settings → Devices & services → Add integration → Msheireb Smart Home**. Enter your portal email and password once. Tokens are stored in the config entry and refreshed automatically (24 h access token, rotating 7-day refresh token). If the refresh token expires, the integration logs in again with the stored credentials. If the password changes, Home Assistant prompts you to re-authenticate.

**Options** (Configure button): min/max target temperature (default 18–30 °C), polling interval (default 30 s), pulse spacing (default 5.0 s, range 0.5–10 s), automatic retries (default 2, range 0–5), on external change (default restore + notify), external-change grace period (default 60 s), adopt external changes (default off), notifications on/off (default on).

A command counts as *confirmed* when a later poll shows the requested state (setpoint, power or fan). The confirmation window scales with the number of presses: **presses × pulse spacing + 20 s**, counted from the first press. For example, a 2 °C change is 4 presses, which gives 4 × 5 s + 20 s = 40 s. If the state doesn't match within that window, the integration **retries automatically** (default 2 retries, configurable 0–5). It re-reads the actual state first. For temperature, it sends only the presses still needed from the actual setpoint. For power and fan, it re-sends the press only if the actual state still differs, so it never toggles blindly. Each retry gets its own window (retry presses × spacing + 20 s). Only after the final retry fails does the command become *not confirmed*, which increments "Commands failed" and sends a notification. Counters reset when Home Assistant restarts.

## Notes / limitations
- Commands are queued by the cloud. A successful API call does not guarantee execution, so state is confirmed by polling.
- The direct setpoint write (`A1`) is accepted by the API but ignored by the controller, so pulses are used instead.
- Only Cool/Off is exposed. The controller reports no heating or dry modes.
- Your email and password are stored in Home Assistant's config entry storage (`.storage/core.config_entries`), like other cloud integrations that need re-login.

## Development
```
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```
