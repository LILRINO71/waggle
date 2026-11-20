"""Core process settings: files in the Waggle home, the health port, worker count and loop cadences.

Values come from ~/.waggle/config.json (keys core_port, core_workers) with defaults; environment variables
WAGGLE_CORE_PORT, WAGGLE_CORE_WORKERS and WAGGLE_CORE_CADENCE ("heartbeat=1,scheduler=0.5") override them,
which is how the tests run a fast core in a temporary home."""
import json
import os
from dataclasses import dataclass, fields, replace
from pathlib import Path

import waggle.config as config

REPO = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 8896
DEFAULT_WORKERS = 4
DEFAULT_INBOUND_CHATS = 8


def home():
    return Path(config.HOME)


def path(name):
    """A core file in the Waggle home: core.lock, core.pid, core.stop, core.token, core.log, jobrun.log."""
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    return h / name


def _config_value(key, default):
    try:
        data = json.loads(Path(config.CONFIG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data.get(key, default)


def port():
    return int(os.environ.get("WAGGLE_CORE_PORT") or _config_value("core_port", DEFAULT_PORT))


def workers():
    return max(1, int(os.environ.get("WAGGLE_CORE_WORKERS") or _config_value("core_workers", DEFAULT_WORKERS)))


def inbound_chats():
    """How many chats may run turns at the same time."""
    return max(1, int(os.environ.get("WAGGLE_CORE_INBOUND_CHATS")
                      or _config_value("core_inbound_chats", DEFAULT_INBOUND_CHATS)))


@dataclass(frozen=True)
class Cadence:
    heartbeat: float = 15.0  # meta.heartbeat write
    scheduler: float = 5.0  # timers.fire_due
    poll: float = 1.0  # idle wait of workers, the detached dispatcher and the sender
    leases: float = 5.0  # lease renewal and timeout checks of running jobs
    housekeeping: float = 60.0  # jobs.recover, outbox.recover, outbox.expire
    guard: float = 3600.0  # guard.verify
    stop_check: float = 0.5  # how often the main loop looks for the stop file
    send_grace: float = 5.0  # how long a graceful stop waits for in-flight work

    @classmethod
    def from_env(cls):
        base = cls()
        text = os.environ.get("WAGGLE_CORE_CADENCE", "")
        known = {f.name for f in fields(cls)}
        changes = {}
        for item in text.split(","):
            key, _, value = item.partition("=")
            if key.strip() in known and value.strip():
                changes[key.strip()] = float(value)
        return replace(base, **changes)
