"""Find modpacks that share mods with a chosen pack: /api/similar/..."""

from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..models import Modpack, Source
from ..services.similar_index import get_index

router = APIRouter(prefix="/api/similar", tags=["similar"])


class ModInfo(BaseModel):
    id: str
    title: str
    slug: str | None = None
    icon_url: str | None = None


class SimilarPack(BaseModel):
    pack: Modpack
    score: float  # weighted Jaccard similarity, 0..1
    shared_count: int
    mod_count: int
    shared_mods: list[ModInfo]


class IndexStatus(BaseModel):
    state: str
    done: int
    total: int
    message: str
    started_at: float | None
    last_refresh: float | None
    indexed: int


class SimilarResult(BaseModel):
    pack: Modpack | None
    mod_count: int
    total: int
    offset: int
    limit: int
    results: list[SimilarPack]
    index: IndexStatus


@router.get("/status", response_model=IndexStatus)
async def status():
    index = get_index()
    index.db()
    return index.status.as_dict()


@router.post("/refresh", response_model=IndexStatus)
async def refresh():
    index = get_index()
    index.db()
    index.start_refresh()
    return index.status.as_dict()


@router.get("/{source}/{pack_id}", response_model=SimilarResult)
async def similar(
    source: Source,
    pack_id: str,
    game_version: str | None = None,
    loader: str | None = None,
    sort: Literal["similarity", "shared"] = "similarity",
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=100),
):
    if source != "modrinth":
        raise HTTPException(status_code=400, detail="Finding similar packs works with Modrinth packs for now.")
    index = get_index()
    # First use, or the index is stale: crawl in the background and serve what we have.
    if index.needs_refresh():
        index.start_refresh()
    try:
        pack = await index.ensure_pack(pack_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"modrinth request failed: {exc}") from exc
    if pack is None:
        raise HTTPException(status_code=404, detail="Modpack not found on Modrinth")
    found = index.similar(pack.id, game_version or None, loader or None, sort, offset, limit)
    wanted = [m for r in found["results"] for m in r["shared_mod_ids"]]
    names = index.mod_names(list(dict.fromkeys(wanted)))
    results = [
        SimilarPack(
            pack=r["pack"],
            score=r["score"],
            shared_count=r["shared_count"],
            mod_count=r["mod_count"],
            shared_mods=[names.get(m) or {"id": m, "title": m} for m in r["shared_mod_ids"]],
        )
        for r in found["results"]
    ]
    return SimilarResult(
        pack=pack,
        mod_count=found["mod_count"],
        total=found["total"],
        offset=offset,
        limit=limit,
        results=results,
        index=index.status.as_dict(),
    )
