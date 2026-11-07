"""Settings the user can change from the web UI, persisted as JSON in the data dir.

Environment variables (see config.py) are the fallback: a value saved from the UI
wins over the env var, and clearing it in the UI falls back to the env var again.
"""

import json
import os
import threading
from pathlib import Path

from .config import settings

_lock = threading.Lock()


def _path() -> Path:
    return Path(settings.data_dir) / "settings.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(updates: dict) -> dict:
    """Merge updates into the stored settings. A value of None removes the key."""
    with _lock:
        data = load()
        for key, value in updates.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)  # holds the CurseForge API key
        tmp.replace(path)
        return data


def curseforge_api_key() -> str:
    """The CurseForge key to use right now: UI setting first, then CF_API_KEY."""
    return (load().get("curseforge_api_key") or settings.curseforge_api_key or "").strip()


def curseforge_key_source() -> str | None:
    if (load().get("curseforge_api_key") or "").strip():
        return "settings"
    if settings.curseforge_api_key.strip():
        return "env"
    return None
