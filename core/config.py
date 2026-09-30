"""Config stored in config/settings.json next to the app."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = base_dir()
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "settings.json"
DATA_DIR = BASE_DIR / "data"

DEFAULTS: dict = {
    "nickname": "",            # the assistant's name, chosen at setup
    "user_name": "",
    "provider": "",            # anthropic | gemini | openai-compatible preset id
    "api_key": "",
    "base_url": "",
    "model": "",
    "voice": "en-IN-PrabhatNeural",
    "language": "en-IN",       # speech recognition language
    "theme": "cyan",
    "voice_enabled": True,     # speak replies aloud
    "auto_listen": False,      # keep the mic open between turns
}

_lock = threading.Lock()


def load() -> dict:
    with _lock:
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in data.items() if k in DEFAULTS})
    return out


def save(values: dict) -> dict:
    cfg = load()
    cfg.update({k: v for k, v in values.items() if k in DEFAULTS})
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def is_configured(cfg: dict | None = None) -> bool:
    cfg = cfg or load()
    needs_key = cfg.get("provider") not in ("ollama", "lmstudio")
    return bool(cfg.get("nickname") and cfg.get("provider") and cfg.get("model")
                and (cfg.get("api_key") or not needs_key))
