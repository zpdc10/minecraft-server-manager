"""Modrinth API v2 client for modpack search. No API key needed."""

import json
import time

import httpx

from ..config import USER_AGENT, settings
from ..models import FilterOptions, Modpack, PackVersion, SearchResult

LOADERS = {"forge", "neoforge", "fabric", "quilt"}
SORTS = {"relevance", "downloads", "follows", "newest", "updated"}

_cache: dict[str, tuple[float, object]] = {}
CACHE_SECONDS = 3600


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.modrinth_api_url,
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )


def build_facets(game_version: str | None, loader: str | None, category: str | None) -> str:
    # Facets are AND-ed lists of OR-ed strings. Client-only packs are kept so a
    # search by name still finds them; they're flagged with server_supported=False.
    facets = [["project_type:modpack"]]
    if game_version:
        facets.append([f"versions:{game_version}"])
    if loader:
        facets.append([f"categories:{loader}"])
    if category:
        facets.append([f"categories:{category}"])
    return json.dumps(facets)


def _to_modpack(hit: dict) -> Modpack:
    cats = hit.get("categories", []) + hit.get("display_categories", [])
    loaders = sorted({c for c in cats if c in LOADERS})
    others = sorted({c for c in cats if c not in LOADERS})
    return Modpack(
        source="modrinth",
        id=hit["project_id"],
        slug=hit["slug"],
        title=hit["title"],
        description=hit.get("description", ""),
        author=hit.get("author", ""),
        icon_url=hit.get("icon_url") or None,
        downloads=hit.get("downloads", 0),
        follows=hit.get("follows", 0),
        loaders=loaders,
        categories=others,
        game_versions=hit.get("versions", []),
        updated=hit.get("date_modified"),
        url=f"https://modrinth.com/modpack/{hit['slug']}",
        server_supported=hit.get("server_side") != "unsupported",
    )


async def _cached_get(path: str):
    now = time.time()
    hit = _cache.get(path)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    async with _client() as client:
        resp = await client.get(path)
        resp.raise_for_status()
        data = resp.json()
    _cache[path] = (now, data)
    return data


class ModrinthProvider:
    name = "modrinth"

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
        params = {
            "query": query,
            "facets": build_facets(game_version, loader, category),
            "index": sort if sort in SORTS else "relevance",
            "offset": offset,
            "limit": limit,
        }
        async with _client() as client:
            resp = await client.get("/search", params=params)
            resp.raise_for_status()
            data = resp.json()
        return SearchResult(
            hits=[_to_modpack(h) for h in data.get("hits", [])],
            total=data.get("total_hits", 0),
            offset=data.get("offset", offset),
            limit=data.get("limit", limit),
        )

    async def filter_options(self) -> FilterOptions:
        versions = await _cached_get("/tag/game_version")
        loaders = await _cached_get("/tag/loader")
        categories = await _cached_get("/tag/category")
        return FilterOptions(
            # Modrinth returns newest first; keep only full releases.
            game_versions=[v["version"] for v in versions if v.get("version_type") == "release"],
            loaders=sorted(
                ldr["name"]
                for ldr in loaders
                if "modpack" in ldr.get("supported_project_types", []) and ldr["name"] in LOADERS
            ),
            categories=sorted(
                {
                    c["name"]
                    for c in categories
                    if c.get("project_type") == "modpack" and c.get("header") == "categories"
                }
            ),
        )

    async def get_versions(self, project_id: str) -> list[PackVersion]:
        async with _client() as client:
            resp = await client.get(f"/project/{project_id}/version")
            resp.raise_for_status()
            data = resp.json()
        return [
            PackVersion(
                id=v["id"],
                name=v.get("name", v["version_number"]),
                version_number=v["version_number"],
                game_versions=v.get("game_versions", []),
                loaders=v.get("loaders", []),
                published=v.get("date_published"),
            )
            for v in data
        ]
