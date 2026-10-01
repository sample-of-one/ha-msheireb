"""Constants for the Msheireb Smart Home integration."""
from __future__ import annotations

DOMAIN = "msheireb"

API_BASE = "https://mob-prod.mp-mdd.com:8443/api"
WEB_ORIGIN = "https://web-prod.mp-mdd.com"

CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_EXPIRES_AT = "expires_at"
CONF_USER_ID = "user_id"
CONF_CONTRACTS = "contracts"

_CONTRACT_FIELDS = ("id", "unitId", "unitName", "externalId", "type", "status", "startDate", "endDate")


def slim_contracts(contracts: list) -> list:
    """Keep only the contract fields the integration needs."""
    return [{k: c[k] for k in _CONTRACT_FIELDS if k in c} for c in contracts if isinstance(c, dict) and c.get("id") is not None]

CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_MIN_TEMP = 18.0
DEFAULT_MAX_TEMP = 30.0
DEFAULT_SCAN_INTERVAL = 30  # seconds

TEMP_STEP = 0.5  # one Temp Up/Down pulse = 0.5 C (measured: 1950 -> 2000)
RAW_TEMP_SCALE = 100  # API reports C x 100
PULSE_INTERVAL = 5.0  # default; see CONF_PULSE_INTERVAL
PULSES_BETWEEN_READS = 3  # re-read setpoint every N pulses
REFRESH_AFTER_COMMAND = 5.0  # seconds
OPTIMISTIC_TIMEOUT = 25.0  # seconds before an unconfirmed optimistic value is dropped
TOKEN_REFRESH_MARGIN = 600  # refresh access token this many seconds before expiry
DEFAULT_TOKEN_LIFETIME = 86400

PULSE_VALUE = "PULSE"

# Logical roles, discovered from control/status labels returned by the API.
ROLE_POWER = "power"
ROLE_FAN_AUTO = "auto"
ROLE_FAN_LOW = "low"
ROLE_FAN_MEDIUM = "medium"
ROLE_FAN_HIGH = "high"
ROLE_TEMP_UP = "temp_up"
ROLE_TEMP_DOWN = "temp_down"
ROLE_SETPOINT = "setpoint"
ROLE_ROOM_TEMP = "room_temp"

FAN_ROLES = (ROLE_FAN_AUTO, ROLE_FAN_LOW, ROLE_FAN_MEDIUM, ROLE_FAN_HIGH)

CONF_NOTIFICATIONS = "notifications"
CONF_PULSE_INTERVAL = "pulse_interval"
DEFAULT_NOTIFICATIONS = True
DEFAULT_PULSE_INTERVAL = 5.0  # seconds between pulse sends (start-to-start); option range 0.5-10 s
PULSE_INTERVAL_MIN = 0.5
PULSE_INTERVAL_MAX = 10.0

CONFIRM_TIMEOUT = 20.0  # base seconds to confirm; effective = pulses x spacing + this
ALERT_AFTER = 300.0  # seconds of controller-disconnected / portal-unreachable before notifying

CMD_PENDING = "pending"
CMD_CONFIRMED = "confirmed"
CMD_NOT_CONFIRMED = "not_confirmed"
CMD_FAILED = "failed"
CMD_RESULTS = [CMD_PENDING, CMD_CONFIRMED, CMD_NOT_CONFIRMED, CMD_FAILED]

CONF_MAX_RETRIES = "max_retries"
DEFAULT_MAX_RETRIES = 2
MAX_RETRIES_LIMIT = 5

# Drift detection ("On external change")
CONF_EXTERNAL_CHANGE = "external_change"
CONF_DRIFT_GRACE = "drift_grace"
CONF_ADOPT_EXTERNAL = "adopt_external"
EXT_NOTIFY = "notify"
EXT_RESTORE = "restore"
EXT_RESTORE_NOTIFY = "restore_notify"
EXT_IGNORE = "ignore"
EXT_MODES = [EXT_RESTORE_NOTIFY, EXT_RESTORE, EXT_NOTIFY, EXT_IGNORE]
DEFAULT_EXTERNAL_CHANGE = EXT_RESTORE_NOTIFY
DEFAULT_DRIFT_GRACE = 60  # seconds of sustained drift (and >= DRIFT_MIN_POLLS polls) before acting
DEFAULT_ADOPT_EXTERNAL = False
DRIFT_MIN_POLLS = 2
STORE_VERSION = 1
