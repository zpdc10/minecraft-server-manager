"""Local index of modpack mod lists, used to find packs that share mods.

Neither Modrinth nor CurseForge can answer "which packs contain mod X", so we build
that ourselves: crawl the most downloaded modpacks, read the mod list from each pack's
latest version (the modrinth.index.json inside the .mrpack), and keep it in SQLite in
the data dir. Similarity is then computed locally.

Scoring is a weighted Jaccard index: shared mods / all mods across both packs, where
each mod is weighted by how rare it is across the index (inverse document frequency).
That way two packs sharing Create and its addons count as more alike than two packs
that only share libraries such as Fabric API or Cloth Config, which nearly every pack has.

Only Modrinth packs are indexed for now. Mod ids are per source, so CurseForge packs
would need their own crawl (with an API key); the tables already carry a source column.
"""

import asyncio
import json
import logging
import math
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import USER_AGENT, settings
from ..models import Modpack
from ..providers.modrinth import _to_modpack
from .remote_zip import RemoteZipError, read_member

log = logging.getLogger(__name__)

SOURCE = "modrinth"
# Mod files hosted on Modrinth: https://cdn.modrinth.com/data/<project id>/versions/<version id>/<file>
CDN_PROJECT = re.compile(r"^https://cdn\.modrinth\.com/data/([A-Za-z0-9]{8})/")
REFRESH_AFTER_SECONDS = 7 * 24 * 3600
API_DELAY = 0.25  # stay well under Modrinth's 300 requests/minute
CONCURRENCY = 4
SCHEMA = """
CREATE TABLE IF NOT EXISTS packs (
    source TEXT NOT NULL, id TEXT NOT NULL, version_id TEXT, pack_json TEXT NOT NULL,
    mod_count INTEGER NOT NULL DEFAULT 0, indexed_at REAL NOT NULL, error TEXT,
    PRIMARY KEY (source, id)
);
CREATE TABLE IF NOT EXISTS pack_mods (
    source TEXT NOT NULL, pack_id TEXT NOT NULL, mod_id TEXT NOT NULL,
    PRIMARY KEY (source, pack_id, mod_id)
);
CREATE INDEX IF NOT EXISTS pack_mods_mod ON pack_mods (source, mod_id);
CREATE TABLE IF NOT EXISTS mods (
    source TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, slug TEXT, icon_url TEXT,
    PRIMARY KEY (source, id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def mod_ids_from_index(index: dict) -> set[str]:
    """Modrinth project ids of the mods listed in a modrinth.index.json."""
    ids = set()
    for f in index.get("files", []):
        if not str(f.get("path", "")).replace("\\", "/").startswith("mods/"):
            continue
        for url in f.get("downloads", []):
            m = CDN_PROJECT.match(url)
            if m:
                ids.add(m.group(1))
                break
    return ids


@dataclass
class Status:
    state: str = "idle"  # idle | running | error
    done: int = 0
    total: int = 0
    message: str = ""
    started_at: float | None = None
    last_refresh: float | None = None
    indexed: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class _Cache:
    stamp: int = -1
    packs: dict[str, Modpack] = field(default_factory=dict)
    mods: dict[str, frozenset[str]] = field(default_factory=dict)
    idf: dict[str, float] = field(default_factory=dict)


class SimilarIndex:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(settings.data_dir) / "similar-index.sqlite3"
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._task: asyncio.Task | None = None
        self.status = Status()
        self._cache = _Cache()
        self._changed = 0  # bumped on every write; the in-memory cache compares against it

    # ---------- storage ----------

    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.executescript(SCHEMA)
            row = self._db.execute("SELECT value FROM meta WHERE key='last_refresh'").fetchone()
            self.status.last_refresh = float(row[0]) if row else None
            self.status.indexed = self._db.execute(
                "SELECT COUNT(*) FROM packs WHERE source=? AND mod_count>0", (SOURCE,)
            ).fetchone()[0]
        return self._db

    def _store_pack(self, pack: Modpack, version_id: str | None, mods: set[str], error: str | None = None):
        with self._lock:
            db = self.db()
            db.execute("DELETE FROM pack_mods WHERE source=? AND pack_id=?", (SOURCE, pack.id))
            db.executemany(
                "INSERT INTO pack_mods (source, pack_id, mod_id) VALUES (?, ?, ?)",
                [(SOURCE, pack.id, m) for m in mods],
            )
            db.execute(
                "INSERT OR REPLACE INTO packs (source, id, version_id, pack_json, mod_count, indexed_at, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (SOURCE, pack.id, version_id, pack.model_dump_json(), len(mods), time.time(), error),
            )
            db.commit()
            self.status.indexed = db.execute(
                "SELECT COUNT(*) FROM packs WHERE source=? AND mod_count>0", (SOURCE,)
            ).fetchone()[0]
            self._changed += 1

    def _known_version(self, pack_id: str) -> str | None:
        with self._lock:
            row = (
                self.db()
                .execute("SELECT version_id FROM packs WHERE source=? AND id=? AND error IS NULL", (SOURCE, pack_id))
                .fetchone()
            )
        return row[0] if row else None

    def _load(self) -> _Cache:
        if self._cache.stamp >= self._changed:
            return self._cache
        with self._lock:
            db = self.db()
            stamp = self._changed
            packs = {
                pid: Modpack.model_validate_json(js)
                for pid, js in db.execute("SELECT id, pack_json FROM packs WHERE source=? AND mod_count>0", (SOURCE,))
            }
            sets: dict[str, set[str]] = {}
            for pid, mid in db.execute("SELECT pack_id, mod_id FROM pack_mods WHERE source=?", (SOURCE,)):
                sets.setdefault(pid, set()).add(mid)
        df: dict[str, int] = {}
        for mods in sets.values():
            for m in mods:
                df[m] = df.get(m, 0) + 1
        n = max(len(sets), 1)
        self._cache = _Cache(
            stamp=stamp,
            packs=packs,
            mods={k: frozenset(v) for k, v in sets.items()},
            idf={m: math.log((n + 1) / c) for m, c in df.items()},
        )
        return self._cache

    def mod_names(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        out = {}
        with self._lock:
            db = self.db()
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                marks = ",".join("?" * len(chunk))
                q = f"SELECT id, title, slug, icon_url FROM mods WHERE source=? AND id IN ({marks})"
                for mid, title, slug, icon in db.execute(q, (SOURCE, *chunk)):
                    out[mid] = {"id": mid, "title": title, "slug": slug, "icon_url": icon}
        return out

    def _store_mod_names(self, projects: list[dict]):
        with self._lock:
            db = self.db()
            db.executemany(
                "INSERT OR REPLACE INTO mods (source, id, title, slug, icon_url) VALUES (?, ?, ?, ?, ?)",
                [
                    (SOURCE, p["id"], p.get("title") or p["id"], p.get("slug"), p.get("icon_url") or None)
                    for p in projects
                ],
            )
            db.commit()

    # ---------- similarity ----------

    def similar(
        self,
        pack_id: str,
        game_version: str | None = None,
        loader: str | None = None,
        sort: str = "similarity",
        offset: int = 0,
        limit: int = 24,
    ) -> dict:
        cache = self._load()
        target = cache.mods.get(pack_id)
        if not target:
            return {"total": 0, "results": [], "mod_count": 0}
        idf = cache.idf
        target_w = sum(idf.get(m, 0) for m in target)
        scored = []
        for pid, mods in cache.mods.items():
            if pid == pack_id:
                continue
            pack = cache.packs.get(pid)
            if pack is None:
                continue
            if game_version and game_version not in pack.game_versions:
                continue
            if loader and loader not in pack.loaders:
                continue
            shared = target & mods
            if not shared:
                continue
            shared_w = sum(idf.get(m, 0) for m in shared)
            union_w = target_w + sum(idf.get(m, 0) for m in mods) - shared_w
            score = shared_w / union_w if union_w else 0
            scored.append((score, len(shared), pid, shared))
        if sort == "shared":
            scored.sort(key=lambda s: (s[1], s[0]), reverse=True)
        else:
            scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        page = scored[offset : offset + limit]
        results = []
        for score, n_shared, pid, shared in page:
            # Rarest shared mods first: those say most about why the packs are alike.
            ordered = sorted(shared, key=lambda m: idf.get(m, 0), reverse=True)
            results.append(
                {
                    "pack": cache.packs[pid],
                    "score": round(score, 4),
                    "shared_count": n_shared,
                    "mod_count": len(cache.mods[pid]),
                    "shared_mod_ids": ordered,
                }
            )
        return {"total": len(scored), "results": results, "mod_count": len(target)}

    # ---------- crawling ----------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=settings.modrinth_api_url,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
            follow_redirects=True,
        )

    async def _api(self, client: httpx.AsyncClient, path: str, params: dict | None = None):
        for attempt in range(4):
            await asyncio.sleep(API_DELAY)
            resp = await client.get(path, params=params)
            if resp.status_code == 429:
                wait = float(resp.headers.get("X-Ratelimit-Reset", 10 * (attempt + 1)))
                await asyncio.sleep(min(wait, 60))
                continue
            resp.raise_for_status()
            return resp.json()
        raise httpx.HTTPError(f"Modrinth kept rate limiting {path}")

    async def index_pack(self, client: httpx.AsyncClient, pack: Modpack, version_id: str | None) -> set[str]:
        """Fetch and store the mod list of one pack's version."""
        if not version_id:
            versions = await self._api(client, f"/project/{pack.id}/version")
            if not versions:
                self._store_pack(pack, None, set(), "no versions")
                return set()
            version = max(versions, key=lambda v: v.get("date_published") or "")
        else:
            version = await self._api(client, f"/version/{version_id}")
        mods = {d["project_id"] for d in version.get("dependencies", []) if d.get("project_id")}
        files = version.get("files", [])
        mrpack = next((f for f in files if f.get("primary")), files[0] if files else None)
        error = None
        if mrpack:
            try:
                raw = await read_member(client, mrpack["url"], "modrinth.index.json")
                if raw:
                    mods |= mod_ids_from_index(json.loads(raw))
            except (RemoteZipError, httpx.HTTPError, ValueError) as exc:
                error = str(exc) or type(exc).__name__
                log.warning("could not read %s: %s", pack.slug, error)
        mods.discard(pack.id)
        self._store_pack(pack, version["id"], mods, None if mods else (error or "no mods found"))
        return mods

    async def ensure_pack(self, pack_id: str) -> Modpack | None:
        """Make sure one pack (e.g. the one the user picked) is in the index."""
        cache = self._load()
        if pack_id in cache.mods:
            return cache.packs[pack_id]
        async with self._client() as client:
            data = await self._api(
                client,
                "/search",
                {"facets": json.dumps([["project_type:modpack"], [f"project_id:{pack_id}"]]), "limit": 1},
            )
            hits = data.get("hits", [])
            if not hits:
                # The id may be a slug; resolve it.
                project = await self._api(client, f"/project/{pack_id}")
                return await self.ensure_pack(project["id"]) if project.get("id") != pack_id else None
            hit = hits[0]
            pack = _to_modpack(hit)
            mods = await self.index_pack(client, pack, hit.get("latest_version"))
            await self._resolve_names(client, mods)
        return pack

    async def _resolve_names(self, client: httpx.AsyncClient, ids: set[str]):
        known = self.mod_names(list(ids))
        missing = [m for m in ids if m not in known]
        for i in range(0, len(missing), 100):
            chunk = missing[i : i + 100]
            try:
                projects = await self._api(client, "/projects", {"ids": json.dumps(chunk)})
            except httpx.HTTPError as exc:
                log.warning("could not fetch mod names: %s", exc)
                return
            self._store_mod_names(projects)

    def needs_refresh(self) -> bool:
        self.db()
        last = self.status.last_refresh
        return last is None or time.time() - last > REFRESH_AFTER_SECONDS

    def start_refresh(self) -> bool:
        """Start a background crawl if one isn't already running. Returns True if started."""
        if self._task and not self._task.done():
            return False
        self.status = Status(
            state="running",
            started_at=time.time(),
            last_refresh=self.status.last_refresh,
            indexed=self.status.indexed,
            message="Listing modpacks…",
        )
        self._task = asyncio.get_running_loop().create_task(self._refresh())
        return True

    async def _refresh(self):
        try:
            async with self._client() as client:
                hits: list[dict] = []
                size = max(1, min(10_000, settings.similar_index_size))
                facets = json.dumps([["project_type:modpack"], ["server_side!=unsupported"]])
                while len(hits) < size:
                    data = await self._api(
                        client, "/search", {"facets": facets, "index": "downloads", "offset": len(hits), "limit": 100}
                    )
                    page = data.get("hits", [])
                    hits.extend(page)
                    if len(page) < 100:
                        break
                hits = hits[:size]
                self.status.total = len(hits)
                self.status.message = "Reading mod lists…"
                queue: asyncio.Queue = asyncio.Queue()
                for h in hits:
                    queue.put_nowait(h)

                async def worker():
                    while True:
                        try:
                            hit = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        pack = _to_modpack(hit)
                        try:
                            if hit.get("latest_version") and self._known_version(pack.id) == hit["latest_version"]:
                                # Unchanged since last crawl: just refresh the pack details.
                                with self._lock:
                                    self.db().execute(
                                        "UPDATE packs SET pack_json=? WHERE source=? AND id=?",
                                        (pack.model_dump_json(), SOURCE, pack.id),
                                    )
                                    self.db().commit()
                                    self._changed += 1
                            else:
                                await self.index_pack(client, pack, hit.get("latest_version"))
                        except httpx.HTTPError as exc:
                            log.warning("indexing %s failed: %s", pack.slug, exc)
                        self.status.done += 1

                await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
                self.status.message = "Looking up mod names…"
                with self._lock:
                    every = {
                        r[0]
                        for r in self.db().execute("SELECT DISTINCT mod_id FROM pack_mods WHERE source=?", (SOURCE,))
                    }
                await self._resolve_names(client, every)
            now = time.time()
            with self._lock:
                self.db().execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_refresh', ?)", (str(now),))
                self.db().commit()
            self.status.state, self.status.message, self.status.last_refresh = "idle", "", now
        except Exception as exc:  # keep the app running; show the problem in the UI
            log.exception("similar-pack index refresh failed")
            self.status.state, self.status.message = "error", f"Index refresh failed: {exc}"


_index: SimilarIndex | None = None


def get_index() -> SimilarIndex:
    global _index
    if _index is None:
        _index = SimilarIndex()
    return _index
