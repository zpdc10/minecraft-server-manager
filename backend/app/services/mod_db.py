"""The list of mods known to be client-only or to crash dedicated servers.

Three sources are merged:

- the shared list fetched from MOD_DB_URL (by default this repo's
  backend/app/data/known-mods.json on GitHub), cached for a few hours;
- the copy of that same file shipped inside the image, used when the fetch fails;
- mods this install learned while building servers (learned-mods.json in DATA_DIR).

A learned mod is one the builder removed because it loaded client-only game code
on the server. Those are safe to remove from every future pack. Mods removed for
a pack-specific crash or a missing dependency are not learned, since they may
work fine in another pack.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..config import USER_AGENT, Settings
from .mod_scan import ModJar

log = logging.getLogger(__name__)

BUNDLED_PATH = Path(__file__).resolve().parent.parent / "data" / "known-mods.json"
REMOTE_CACHE_SECONDS = 6 * 3600


@dataclass
class Match:
    reason: str
    note: str
    source: str  # shared | learned
    matched: str


def _file_rx(token: str) -> re.Pattern:
    if len(token) > 2 and token.startswith("/") and token.endswith("/"):
        return re.compile(token[1:-1], re.I)
    # Match at the start of the file name or after a separator, and not as part of a longer word.
    return re.compile(r"(?:^|[-_ .+])" + re.escape(token) + r"(?![a-z])", re.I)


class ModDB:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._remote: dict | None = None
        self._remote_at = 0.0
        self._remote_error = ""

    # ---- sources -------------------------------------------------------------

    @property
    def _learned_path(self) -> Path:
        return Path(self.settings.data_dir) / "learned-mods.json"

    @property
    def _remote_cache_path(self) -> Path:
        return Path(self.settings.data_dir) / "known-mods-cache.json"

    @staticmethod
    def _load_json(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def bundled(self) -> dict:
        return self._load_json(BUNDLED_PATH) or {"mods": [], "files": []}

    def shared(self) -> tuple[dict, str]:
        """The shared list and where it came from ("remote", "cache" or "bundled")."""
        url = self.settings.mod_db_url
        now = time.time()
        with self._lock:
            if self._remote is not None and now - self._remote_at < REMOTE_CACHE_SECONDS:
                return self._remote, "remote"
            if url and now - self._remote_at >= 300:
                self._remote_at = now
                try:
                    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=10, follow_redirects=True)
                    resp.raise_for_status()
                    data = resp.json()
                    if not isinstance(data, dict) or not isinstance(data.get("mods"), list):
                        raise ValueError("not a known-mods list")
                    self._remote, self._remote_error = data, ""
                    try:
                        self._remote_cache_path.parent.mkdir(parents=True, exist_ok=True)
                        self._remote_cache_path.write_text(json.dumps(data))
                    except OSError:
                        pass
                    return data, "remote"
                except (httpx.HTTPError, ValueError) as e:
                    self._remote_error = str(e)[:200]
                    log.info("Could not fetch the shared mod list from %s: %s", url, e)
            if self._remote is not None:
                return self._remote, "remote"
        cached = self._load_json(self._remote_cache_path)
        if cached and isinstance(cached.get("mods"), list):
            return cached, "cache"
        return self.bundled(), "bundled"

    def learned(self) -> list[dict]:
        data = self._load_json(self._learned_path) or {}
        mods = data.get("mods")
        return mods if isinstance(mods, list) else []

    def _save_learned(self, mods: list[dict]) -> None:
        path = self._learned_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "mods": mods}, indent=1))
        tmp.replace(path)

    # ---- matching --------------------------------------------------------------

    def matcher(self, loader: str = ""):
        """Return a function jar -> Match | None using the current lists."""
        shared, _ = self.shared()
        loader = (loader or "").lower()
        id_map: dict[str, Match] = {}
        file_rules: list[tuple[re.Pattern, Match]] = []

        def applies(entry: dict) -> bool:
            loaders = [str(x).lower() for x in entry.get("loaders") or []]
            return not loaders or not loader or loader in loaders

        for source, entries in (("shared", shared.get("mods") or []), ("learned", self.learned())):
            for e in entries:
                if not isinstance(e, dict) or not applies(e):
                    continue
                for mid in e.get("ids") or []:
                    m = Match(e.get("reason", "client_only"), e.get("note", ""), source, str(mid))
                    id_map.setdefault(str(mid).lower(), m)
        for e in shared.get("files") or []:
            if isinstance(e, dict) and e.get("match") and applies(e):
                m = Match(e.get("reason", "client_only"), e.get("note", ""), "shared", e["match"])
                file_rules.append((_file_rx(e["match"]), m))

        def match(jar: ModJar) -> Match | None:
            for mid in jar.mod_ids:
                if mid.lower() in id_map:
                    return id_map[mid.lower()]
            for rx, m in file_rules:
                if rx.search(jar.filename):
                    return m
            return None

        return match

    def check(self, mod_ids: list[str] | None = None, filename: str = "", loader: str = "") -> Match | None:
        """Is this mod known to be client-only or broken on servers? For callers without a jar."""
        return self.matcher(loader)(ModJar(filename=filename, mod_ids=list(mod_ids or [])))

    def learn(self, jar: ModJar, reason: str, detail: str, pack: str, loader: str) -> None:
        """Remember a mod that broke a server build, so future builds remove it up front."""
        if reason != "client_only" or not jar.mod_ids:
            return
        with self._lock:
            mods = self.learned()
            for e in mods:
                if set(e.get("ids") or []) & set(jar.mod_ids):
                    e["count"] = int(e.get("count", 1)) + 1
                    e["last_seen"] = datetime.now(UTC).isoformat(timespec="seconds")
                    break
            else:
                mods.append(
                    {
                        "ids": jar.mod_ids,
                        "name": jar.name,
                        "file": jar.filename,
                        "reason": reason,
                        "note": detail[:300],
                        "pack": pack,
                        "loader": loader,
                        "count": 1,
                        "first_seen": datetime.now(UTC).isoformat(timespec="seconds"),
                    }
                )
            self._save_learned(mods)

    def forget(self, mod_id: str) -> bool:
        with self._lock:
            mods = self.learned()
            keep = [e for e in mods if mod_id not in (e.get("ids") or [])]
            if len(keep) == len(mods):
                return False
            self._save_learned(keep)
            return True

    def status(self) -> dict:
        shared, source = self.shared()
        return {
            "url": self.settings.mod_db_url,
            "source": source,
            "error": self._remote_error if source != "remote" else "",
            "shared_count": len(shared.get("mods") or []) + len(shared.get("files") or []),
            "learned": self.learned(),
        }


_instance: ModDB | None = None


def get_mod_db(settings: Settings) -> ModDB:
    global _instance
    if _instance is None or _instance.settings is not settings:
        _instance = ModDB(settings)
    return _instance
