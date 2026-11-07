"""CurseForge Core API client for modpack search.

Needs an API key (apply at https://console.curseforge.com). The key is read on every
request from settings_store, so a key saved in the UI takes effect immediately.
API reference: https://docs.curseforge.com/rest-api/
"""

import re
import time

import httpx

from .. import settings_store
from ..config import USER_AGENT, settings
from ..models import FilterOptions, Modpack, PackVersion, SearchResult
from .base import ProviderError

MINECRAFT_GAME_ID = 432
MODPACK_CLASS_ID = 4471
MAX_PAGE_SIZE = 50
# CurseForge rejects searches where index + pageSize > 10000.
MAX_RESULT_WINDOW = 10_000
MAX_VERSION_FILES = 500

# ModLoaderType enum.
LOADER_IDS = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
LOADER_NAMES = {v: k for k, v in LOADER_IDS.items()}

# ModsSearchSortField enum. CurseForge has no relevance sort or follower count, so
# relevance and follows map to popularity, which also ranks text matches sensibly.
SORT_FIELDS = {
    "relevance": 2,  # Popularity
    "downloads": 6,  # TotalDownloads
    "follows": 2,  # Popularity
    "newest": 11,  # ReleasedDate
    "updated": 3,  # LastUpdated
}

RELEASE_VERSION = re.compile(r"^\d+\.\d+(\.\d+)?$")

_cache: dict[str, tuple[float, object]] = {}
CACHE_SECONDS = 3600


class MissingApiKey(ProviderError):
    def __init__(self) -> None:
        super().__init__(
            "CurseForge needs an API key. Add it in Settings (apply for one at https://console.curseforge.com).",
            status_code=400,
        )


def _client(api_key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.curseforge_api_url,
        headers={"User-Agent": USER_AGENT, "x-api-key": api_key, "Accept": "application/json"},
        timeout=15,
    )


async def _get(path: str, params: dict | None = None, api_key: str | None = None) -> dict:
    key = api_key if api_key is not None else settings_store.curseforge_api_key()
    if not key:
        raise MissingApiKey()
    params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    async with _client(key) as client:
        resp = await client.get(path, params=params)
    if resp.status_code in (401, 403):
        raise ProviderError("CurseForge rejected the API key. Check it in Settings.", status_code=400)
    resp.raise_for_status()  # router turns other HTTP errors into 502
    return resp.json()


async def _cached_get(path: str, params: dict | None = None) -> dict:
    cache_key = f"{path}?{sorted((params or {}).items())}"
    now = time.time()
    hit = _cache.get(cache_key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    data = await _get(path, params)
    _cache[cache_key] = (now, data)
    return data


async def _categories() -> dict[str, int]:
    """Modpack category name -> id."""
    data = await _cached_get("/v1/categories", {"gameId": MINECRAFT_GAME_ID, "classId": MODPACK_CLASS_ID})
    return {c["name"]: c["id"] for c in data.get("data", []) if c.get("classId") == MODPACK_CLASS_ID}


def _to_modpack(mod: dict) -> Modpack:
    versions: list[str] = []
    loaders: set[str] = set()
    for idx in mod.get("latestFilesIndexes") or []:
        gv = idx.get("gameVersion")
        if gv and gv not in versions:
            versions.append(gv)
        if idx.get("modLoader") in LOADER_NAMES:
            loaders.add(LOADER_NAMES[idx["modLoader"]])
    authors = mod.get("authors") or []
    logo = mod.get("logo") or {}
    slug = mod.get("slug", "")
    return Modpack(
        source="curseforge",
        id=str(mod["id"]),
        slug=slug,
        title=mod.get("name", ""),
        description=mod.get("summary", ""),
        author=authors[0].get("name", "") if authors else "",
        icon_url=logo.get("thumbnailUrl") or logo.get("url") or None,
        downloads=int(mod.get("downloadCount") or 0),
        follows=int(mod.get("thumbsUpCount") or 0),
        loaders=sorted(loaders),
        categories=sorted({c["name"] for c in mod.get("categories") or [] if c.get("name")}),
        game_versions=versions,
        updated=mod.get("dateModified"),
        url=(mod.get("links") or {}).get("websiteUrl") or f"https://www.curseforge.com/minecraft/modpacks/{slug}",
    )


def _to_version(f: dict) -> PackVersion:
    # gameVersions mixes Minecraft versions ("1.20.1") with loader and side tags ("Forge", "Server").
    tags = f.get("gameVersions") or []
    return PackVersion(
        id=str(f["id"]),
        name=f.get("displayName") or f.get("fileName", ""),
        version_number=f.get("fileName", ""),
        game_versions=[t for t in tags if t[:1].isdigit()],
        loaders=sorted({t.lower() for t in tags if t.lower() in LOADER_IDS}),
        published=f.get("fileDate"),
    )


class CurseForgeProvider:
    name = "curseforge"

    async def search(
        self,
        query: str = "",
        game_version: str | None = None,
        loader: str | None = None,
        category: str | None = None,
        sort: str = "relevance",
        offset: int = 0,
        limit: int = 20,
    ) -> SearchResult:
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        offset = max(0, min(offset, MAX_RESULT_WINDOW - limit))
        category_id = None
        if category:
            category_id = (await _categories()).get(category)
            if category_id is None:
                return SearchResult(hits=[], total=0, offset=offset, limit=limit)
        data = await _get(
            "/v1/mods/search",
            {
                "gameId": MINECRAFT_GAME_ID,
                "classId": MODPACK_CLASS_ID,
                "searchFilter": query,
                "gameVersion": game_version,
                "modLoaderType": LOADER_IDS.get((loader or "").lower()),
                "categoryId": category_id,
                "sortField": SORT_FIELDS.get(sort, 2),
                "sortOrder": "desc",
                "index": offset,
                "pageSize": limit,
            },
        )
        total = int((data.get("pagination") or {}).get("totalCount") or 0)
        return SearchResult(
            hits=[_to_modpack(m) for m in data.get("data") or []],
            # Only the first 10k results are reachable, so don't offer pages past that.
            total=min(total, MAX_RESULT_WINDOW),
            offset=offset,
            limit=limit,
        )

    async def filter_options(self) -> FilterOptions:
        versions = await _cached_get("/v1/minecraft/version", {"sortDescending": "true"})
        return FilterOptions(
            game_versions=[
                v["versionString"]
                for v in versions.get("data", [])
                if RELEASE_VERSION.match(v.get("versionString", ""))
            ],
            loaders=sorted(LOADER_IDS),
            categories=sorted(await _categories()),
        )

    async def get_versions(self, project_id: str) -> list[PackVersion]:
        if not project_id.isdigit():
            raise ProviderError("CurseForge project ids are numeric.", status_code=400)
        files: list[dict] = []
        while len(files) < MAX_VERSION_FILES:
            data = await _get(f"/v1/mods/{project_id}/files", {"index": len(files), "pageSize": MAX_PAGE_SIZE})
            batch = data.get("data") or []
            files.extend(batch)
            total = int((data.get("pagination") or {}).get("totalCount") or 0)
            if not batch or len(files) >= total:
                break
        files.sort(key=lambda f: f.get("fileDate") or "", reverse=True)
        # Server packs are attached to client files; itzg picks the server pack itself.
        return [_to_version(f) for f in files if not f.get("isServerPack")]


async def check_api_key(api_key: str) -> bool:
    """True if CurseForge accepts the key. Network errors propagate."""
    try:
        await _get(f"/v1/games/{MINECRAFT_GAME_ID}", api_key=api_key)
    except ProviderError:
        return False
    return True
