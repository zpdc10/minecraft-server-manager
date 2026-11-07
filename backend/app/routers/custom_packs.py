"""Pack builder endpoints: mod search, custom pack editing, and launcher exports."""

import asyncio
import json
from collections import defaultdict

import httpx
from fastapi import APIRouter, HTTPException, Query, Response

from ..models import FilterOptions, Source
from ..models.pack import (
    AddModRequest,
    AddModResult,
    CreatePackRequest,
    CustomPack,
    ModSearchResult,
    ModVersion,
    PackLoader,
    PackView,
    RetargetResult,
    UpdatePackRequest,
    mod_key,
)
from ..providers import PROVIDERS, ProviderError
from ..providers.mods import MOD_SOURCES
from ..services import loader_versions, pack_export, pack_resolver, pack_store

router = APIRouter(prefix="/api", tags=["pack builder"])
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _upstream(exc: httpx.HTTPError) -> HTTPException:
    return HTTPException(status_code=502, detail=f"Mod site request failed: {exc}")


def _load(pack_id: str) -> CustomPack:
    try:
        return pack_store.get(pack_id)
    except pack_store.PackNotFound as e:
        raise HTTPException(404, f"Pack '{pack_id}' not found") from e


# ---- mods -------------------------------------------------------------------


@router.get("/mods/search", response_model=ModSearchResult)
async def search_mods(
    source: Source = "modrinth",
    q: str = "",
    game_version: str | None = None,
    loader: str | None = None,
    category: str | None = None,
    sort: str = "relevance",
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
):
    try:
        return await MOD_SOURCES[source].search(q, game_version, loader, category, sort, offset, limit)
    except httpx.HTTPError as exc:
        raise _upstream(exc) from exc


@router.get("/mods/filters", response_model=FilterOptions)
async def mod_filters(source: Source = "modrinth"):
    try:
        packs = await PROVIDERS[source].filter_options()
        return FilterOptions(
            game_versions=packs.game_versions,
            loaders=sorted(pack_resolver.LOADER_COMPAT),
            categories=await MOD_SOURCES[source].categories(),
        )
    except httpx.HTTPError as exc:
        raise _upstream(exc) from exc


@router.get("/mods/{source}/{project_id}/versions", response_model=list[ModVersion])
async def mod_versions(source: Source, project_id: str, game_version: str | None = None, loader: str | None = None):
    try:
        return await MOD_SOURCES[source].versions(project_id, game_version, loader)
    except httpx.HTTPError as exc:
        raise _upstream(exc) from exc


@router.get("/loaders/{loader}/versions", response_model=list[str])
async def get_loader_versions(loader: PackLoader, minecraft_version: str):
    try:
        return await loader_versions.loader_versions(loader, minecraft_version)
    except httpx.HTTPError as exc:
        raise _upstream(exc) from exc


# ---- packs ------------------------------------------------------------------


@router.get("/custom-packs", response_model=list[PackView])
async def list_packs():
    return [pack_resolver.view(p) for p in pack_store.list_packs()]


@router.post("/custom-packs", response_model=PackView, status_code=201)
async def create_pack(req: CreatePackRequest):
    loader_version = req.loader_version or await loader_versions.default_loader_version(
        req.loader, req.minecraft_version
    )
    pack = CustomPack(
        id=pack_store.new_id(req.name),
        name=req.name,
        author=req.author,
        summary=req.summary,
        minecraft_version=req.minecraft_version,
        loader=req.loader,
        loader_version=loader_version,
    )
    return pack_resolver.view(pack_store.save(pack))


@router.get("/custom-packs/{pack_id}", response_model=PackView)
async def get_pack(pack_id: str):
    return pack_resolver.view(_load(pack_id))


@router.put("/custom-packs/{pack_id}", response_model=RetargetResult)
async def update_pack(pack_id: str, req: UpdatePackRequest):
    async with _locks[pack_id]:
        pack = _load(pack_id)
        changes = req.model_dump(exclude_unset=True, exclude_none=True)
        retarget = any(k in changes and changes[k] != getattr(pack, k) for k in ("minecraft_version", "loader"))
        for k, v in changes.items():
            setattr(pack, k, v.strip() if isinstance(v, str) and k != "summary" else v)
        if retarget and "loader_version" not in changes:
            pack.loader_version = await loader_versions.default_loader_version(pack.loader, pack.minecraft_version)
        updated, unavailable = [], []
        if retarget:
            try:
                updated, unavailable = await pack_resolver.retarget(pack)
            except httpx.HTTPError as exc:
                raise _upstream(exc) from exc
        pack_store.save(pack)
        return RetargetResult(pack=pack_resolver.view(pack), updated=updated, unavailable=unavailable)


@router.delete("/custom-packs/{pack_id}", status_code=204)
async def delete_pack(pack_id: str):
    try:
        pack_store.delete(pack_id)
    except pack_store.PackNotFound as e:
        raise HTTPException(404, f"Pack '{pack_id}' not found") from e


@router.post("/custom-packs/{pack_id}/mods", response_model=AddModResult)
async def add_mod(pack_id: str, req: AddModRequest):
    async with _locks[pack_id]:
        pack = _load(pack_id)
        try:
            added, notes = await pack_resolver.add_mod(pack, req)
        except httpx.HTTPError as exc:
            raise _upstream(exc) from exc
        pack_store.save(pack)
        return AddModResult(pack=pack_resolver.view(pack), added=added, notes=notes)


@router.delete("/custom-packs/{pack_id}/mods/{source}/{project_id}", response_model=PackView)
async def remove_mod(pack_id: str, source: Source, project_id: str):
    async with _locks[pack_id]:
        pack = _load(pack_id)
        pack_resolver.remove_mod(pack, mod_key(source, project_id))
        pack_store.save(pack)
        return pack_resolver.view(pack)


EXPORTERS = {"mrpack": pack_export.export_mrpack, "curseforge": pack_export.export_curseforge}
MEDIA_TYPES = {"mrpack": "application/x-modrinth-modpack+zip", "curseforge": "application/zip"}


@router.get("/custom-packs/{pack_id}/export/{fmt}")
async def export_pack(pack_id: str, fmt: str):
    if fmt not in EXPORTERS:
        raise HTTPException(404, "Export format must be 'mrpack' or 'curseforge'")
    pack = _load(pack_id)
    if not pack.mods:
        raise ProviderError("Add some mods before exporting.", status_code=400)
    if not pack.loader_version:
        raise ProviderError(f"Pick a {pack.loader} version before exporting.", status_code=400)
    result = await EXPORTERS[fmt](pack)
    return Response(
        content=result.data,
        media_type=MEDIA_TYPES[fmt],
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            # ensure_ascii keeps the header latin-1 safe
            "X-Pack-Warnings": json.dumps(result.warnings, ensure_ascii=True),
            "Access-Control-Expose-Headers": "X-Pack-Warnings",
        },
    )
