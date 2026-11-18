"""Paths and settings. Everything lives outside AppData on purpose: the Microsoft Store
build of the Claude app redirects AppData writes into its own private folder."""
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("WAGGLE_HOME", Path.home() / ".waggle"))
CONFIG_FILE = HOME / "config.json"


@dataclass
class Settings:
    vault: str = str(Path.home() / "WaggleVault")
    jobs_dir: str = str(Path.home() / "Claude" / "waggle-jobs")
    ollama_url: str = "http://localhost:11434"
    local_model: str = "waggle-4b"
    fallback_coder: str = "hf.co/mradermacher/Huihui-Qwen3-Coder-30B-A3B-Instruct-abliterated-i1-GGUF:Q4_K_M"
    embed_model: str = "nomic-embed-text"
    router_model: str = "sonnet"
    builder_model: str = "opus"
    # Share of the Pro limit Waggle may use, and the point where it stops escalating at all.
    waggle_share: float = 0.30
    stop_at: float = 0.85
    # Starting guess for one 5-hour Pro window, in millions of weighted tokens (see quota.py).
    # Only a guess: it is recalibrated every time the real limit is hit.
    window_units_estimate: float = 6.0
    window_hours: float = 5.0
    search_top_k: int = 6
    quiet_hours: list = field(default_factory=lambda: [23, 7])
    # Proactive loop
    watch_roots: list = field(default_factory=lambda: [str(Path.home() / "Claude")])
    extra_repos: list = field(default_factory=list)
    max_verifications_per_day: int = 4
    stale_days: int = 7
    phone_target: str = "telegram"
    # Saved by install.py: cron scripts run with a trimmed environment and may not see LOCALAPPDATA.
    claude_bin: str = ""
    # Weather: saved the first time the user names a city; imperial = °F / mph.
    home_location: str = ""
    units: str = "imperial"

    @classmethod
    def load(cls):
        data = {}
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self):
        HOME.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def data_path(name):
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME / name
