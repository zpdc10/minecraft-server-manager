"""Mod (not modpack) lookups for the pack builder: search, versions, project info.

Each source implements ModSource. The two clients reuse the HTTP helpers of the
modpack providers, so API keys, caching and error handling behave the same.
"""

import json
from typing import Protocol

from ..models.pack import ModHit, ModSearchResult, ModVersion, Side, mod_key
from . import curseforge, modrinth
from .base import ProviderError

# Which mod loaders' files run on each pack loader. Quilt loads most Fabric mods.
LOADER_COMPAT: dict[str, list[str]] = {
    "fabric": ["fabric"],
    "quilt": ["quilt", "fabric"],
    "forge": ["forge"],
    "neoforge": ["neoforge"],
}
RELEASE_RANK = {"release": 0, "beta": 1, "alpha": 2}


class ModInfo(ModHit):
    """Project details used when a mod is added to a pack."""


class ModSource(Protocol):
    name: str

    async def search(
        self,
        query: str,
        game_version: str | None,
        loader: str | None,
        category: str | None,
        sort: str,
        offset: int,
        limit: int,
    ) -> ModSearchResult: ...

    async def categories(self) -> list[str]: ...

    async def project(self, project_id: str) -> ModInfo: ...

    async def versions(self, project_id: str, game_version: str | None, loader: str | None) -> list[ModVersion]: ...

    async def version(self, version_id: str, project_id: str) -> ModVersion: ...


def pick_best(versions: list[ModVersion]) -> ModVersion | None:
    """Prefer stable releases, then the newest. Versions must already fit the pack."""
    if not versions:
        return None
    return sorted(
        versions,
        key=lambda v: (RELEASE_RANK.get(v.release_type, 3), _neg_date(v.published)),
    )[0]


def _neg_date(published: str | None) -> str:
    # Sort newest first inside an ascending sort by inverting each character.
    return "".join(chr(0x10FFFF - ord(c)) for c in (published or ""))


def _side(value: str | None) -> Side:
    return value if value in ("required", "optional", "unsupported") else "unknown"


# ---------------------------------------------------------------------------- Modrinth


class ModrinthMods:
    name = "modrinth"

    async def _get(self, path: str, params: dict | None = None):
        async with modrinth._client() as client:
            resp = await client.get(path, params=params)
        if resp.status_code == 404:
            raise ProviderError("That mod was not found on Modrinth.", status_code=404)
        resp.raise_for_status()
        return resp.json()

    async def search(self, query, game_version, loader, category, sort, offset, limit) -> ModSearchResult:
        facets: list[list[str]] = [["project_type:mod"]]
        if game_version:
            facets.append([f"versions:{game_version}"])
        if loader:
            facets.append([f"categories:{lo}" for lo in LOADER_COMPAT.get(loader, [loader])])
        if category:
            facets.append([f"categories:{category}"])
        data = await self._get(
            "/search",
            {
                "query": query,
                "facets": json.dumps(facets),
                "index": sort if sort in modrinth.SORTS else "relevance",
                "offset": offset,
                "limit": limit,
            },
        )
        hits = []
        for h in data.get("hits", []):
            base = modrinth._to_modpack(h).model_dump()
            base["url"] = f"https://modrinth.com/mod/{h['slug']}"
            hits.append(
                ModHit(**base, client_side=_side(h.get("client_side")), server_side=_side(h.get("server_side")))
            )
        return ModSearchResult(
            hits=hits, total=data.get("total_hits", 0), offset=data.get("offset", offset), limit=limit
        )

    async def categories(self) -> list[str]:
        cats = await modrinth._cached_get("/tag/category")
        return sorted({c["name"] for c in cats if c.get("project_type") == "mod" and c.get("header") == "categories"})

    async def project(self, project_id: str) -> ModInfo:
        p = await self._get(f"/project/{project_id}")
        cats = p.get("categories", [])
        return ModInfo(
            source="modrinth",
            id=p["id"],
            slug=p.get("slug", ""),
            title=p.get("title", p.get("slug", "")),
            description=p.get("description", ""),
            author="",
            icon_url=p.get("icon_url") or None,
            downloads=p.get("downloads", 0),
            follows=p.get("followers", 0),
            loaders=sorted(set(p.get("loaders") or []) & modrinth.LOADERS),
            categories=sorted(c for c in cats if c not in modrinth.LOADERS),
            game_versions=p.get("game_versions", []),
            updated=p.get("updated"),
            url=f"https://modrinth.com/mod/{p.get('slug', p['id'])}",
            client_side=_side(p.get("client_side")),
            server_side=_side(p.get("server_side")),
        )

    async def versions(self, project_id: str, game_version: str | None, loader: str | None) -> list[ModVersion]:
        params = {}
        if game_version:
            params["game_versions"] = json.dumps([game_version])
        if loader:
            params["loaders"] = json.dumps(LOADER_COMPAT.get(loader, [loader]))
        data = await self._get(f"/project/{project_id}/version", params)
        return [await self._to_version(v) for v in data]

    async def version(self, version_id: str, project_id: str) -> ModVersion:
        v = await self._get(f"/version/{version_id}")
        if project_id and v.get("project_id") != project_id:
            # Accept a slug for project_id by checking the real id via the project.
            real = (await self.project(project_id)).id
            if v.get("project_id") != real:
                raise ProviderError("That version belongs to a different mod.", status_code=400)
        return await self._to_version(v)

    async def _to_version(self, v: dict) -> ModVersion:
        files = v.get("files") or []
        f = next((x for x in files if x.get("primary")), files[0] if files else {})
        requires, incompatible, unknown = [], [], []
        for d in v.get("dependencies") or []:
            kind = d.get("dependency_type")
            if kind not in ("required", "incompatible"):
                continue
            if d.get("project_id"):
                (requires if kind == "required" else incompatible).append(mod_key("modrinth", d["project_id"]))
            elif d.get("version_id"):
                unknown.append((kind, d["version_id"]))
        if unknown:
            # Some dependencies only name a version; look up which project each belongs to.
            ids = json.dumps([vid for _, vid in unknown])
            found = {x["id"]: x["project_id"] for x in await self._get("/versions", {"ids": ids})}
            for kind, vid in unknown:
                if vid in found:
                    (requires if kind == "required" else incompatible).append(mod_key("modrinth", found[vid]))
        hashes = f.get("hashes") or {}
        return ModVersion(
            source="modrinth",
            project_id=v["project_id"],
            id=v["id"],
            name=v.get("name") or v.get("version_number", ""),
            version_number=v.get("version_number", ""),
            game_versions=v.get("game_versions", []),
            loaders=v.get("loaders", []),
            release_type=v.get("version_type", "release"),
            published=v.get("date_published"),
            file_name=f.get("filename", ""),
            download_url=f.get("url"),
            sha1=hashes.get("sha1", ""),
            sha512=hashes.get("sha512", ""),
            size=f.get("size", 0),
            requires=requires,
            incompatible=incompatible,
        )


# -------------------------------------------------------------------------- CurseForge

CF_MOD_CLASS_ID = 6
CF_RELEASE_TYPES = {1: "release", 2: "beta", 3: "alpha"}
CF_REQUIRED = 3
CF_INCOMPATIBLE = 5


def _cf_mod_url(mod: dict) -> str:
    return (mod.get("links") or {}).get(
        "websiteUrl"
    ) or f"https://www.curseforge.com/minecraft/mc-mods/{mod.get('slug')}"


class CurseForgeMods:
    name = "curseforge"

    async def search(self, query, game_version, loader, category, sort, offset, limit) -> ModSearchResult:
        limit = max(1, min(limit, curseforge.MAX_PAGE_SIZE))
        offset = max(0, min(offset, curseforge.MAX_RESULT_WINDOW - limit))
        category_id = None
        if category:
            category_id = (await self._category_ids()).get(category)
            if category_id is None:
                return ModSearchResult(hits=[], total=0, offset=offset, limit=limit)
        data = await curseforge._get(
            "/v1/mods/search",
            {
                "gameId": curseforge.MINECRAFT_GAME_ID,
                "classId": CF_MOD_CLASS_ID,
                "searchFilter": query,
                "gameVersion": game_version,
                "modLoaderType": curseforge.LOADER_IDS.get((loader or "").lower()),
                "categoryId": category_id,
                "sortField": curseforge.SORT_FIELDS.get(sort, 2),
                "sortOrder": "desc",
                "index": offset,
                "pageSize": limit,
            },
        )
        hits = [self._to_hit(m) for m in data.get("data") or []]
        total = int((data.get("pagination") or {}).get("totalCount") or 0)
        return ModSearchResult(hits=hits, total=min(total, curseforge.MAX_RESULT_WINDOW), offset=offset, limit=limit)

    def _to_hit(self, mod: dict) -> ModInfo:
        base = curseforge._to_modpack(mod).model_dump()
        base["url"] = _cf_mod_url(mod)
        return ModInfo(**base)

    async def _category_ids(self) -> dict[str, int]:
        data = await curseforge._cached_get(
            "/v1/categories", {"gameId": curseforge.MINECRAFT_GAME_ID, "classId": CF_MOD_CLASS_ID}
        )
        return {c["name"]: c["id"] for c in data.get("data", []) if c.get("parentCategoryId") == CF_MOD_CLASS_ID}

    async def categories(self) -> list[str]:
        return sorted(await self._category_ids())

    async def project(self, project_id: str) -> ModInfo:
        if not project_id.isdigit():
            raise ProviderError("CurseForge project ids are numeric.", status_code=400)
        data = await curseforge._get(f"/v1/mods/{project_id}")
        return self._to_hit(data["data"])

    async def versions(self, project_id: str, game_version: str | None, loader: str | None) -> list[ModVersion]:
        if not project_id.isdigit():
            raise ProviderError("CurseForge project ids are numeric.", status_code=400)
        loaders = LOADER_COMPAT.get(loader, [loader]) if loader else [None]
        out: list[ModVersion] = []
        for lo in loaders:  # CurseForge filters by one loader at a time
            data = await curseforge._get(
                f"/v1/mods/{project_id}/files",
                {
                    "gameVersion": game_version,
                    "modLoaderType": curseforge.LOADER_IDS.get(lo) if lo else None,
                    "pageSize": curseforge.MAX_PAGE_SIZE,
                },
            )
            out.extend(self._to_version(f) for f in data.get("data") or [] if not f.get("isServerPack"))
            if out:
                break
        return out

    async def version(self, version_id: str, project_id: str) -> ModVersion:
        if not (project_id.isdigit() and version_id.isdigit()):
            raise ProviderError("CurseForge ids are numeric.", status_code=400)
        data = await curseforge._get(f"/v1/mods/{project_id}/files/{version_id}")
        return self._to_version(data["data"])

    @staticmethod
    def _to_version(f: dict) -> ModVersion:
        tags = f.get("gameVersions") or []
        sha1 = next((h["value"] for h in f.get("hashes") or [] if h.get("algo") == 1), "")
        deps = f.get("dependencies") or []
        return ModVersion(
            source="curseforge",
            project_id=str(f["modId"]),
            id=str(f["id"]),
            name=f.get("displayName") or f.get("fileName", ""),
            version_number=f.get("displayName") or f.get("fileName", ""),
            game_versions=[t for t in tags if t[:1].isdigit()],
            loaders=sorted({t.lower() for t in tags if t.lower() in curseforge.LOADER_IDS}),
            release_type=CF_RELEASE_TYPES.get(f.get("releaseType"), "release"),
            published=f.get("fileDate"),
            file_name=f.get("fileName", ""),
            # None when the author opted out of third-party downloads.
            download_url=f.get("downloadUrl"),
            sha1=sha1,
            size=int(f.get("fileLength") or 0),
            requires=[mod_key("curseforge", str(d["modId"])) for d in deps if d.get("relationType") == CF_REQUIRED],
            incompatible=[
                mod_key("curseforge", str(d["modId"])) for d in deps if d.get("relationType") == CF_INCOMPATIBLE
            ],
        )


MOD_SOURCES: dict[str, ModSource] = {"modrinth": ModrinthMods(), "curseforge": CurseForgeMods()}
