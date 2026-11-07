"""Modpack search endpoints: /api/search and /api/filters."""

import httpx
from fastapi import APIRouter, HTTPException, Query

from ..models import FilterOptions, PackVersion, SearchResult, Source
from ..providers import PROVIDERS

router = APIRouter(prefix="/api", tags=["search"])


def _provider(source: Source):
    provider = PROVIDERS.get(source)
    if provider is None:
        raise HTTPException(status_code=400, detail=f"Source '{source}' is not available yet")
    return provider


@router.get("/sources")
async def sources() -> list[str]:
    return list(PROVIDERS)


@router.get("/search", response_model=SearchResult)
async def search(
    source: Source = "modrinth",
    q: str = "",
    game_version: str | None = None,
    loader: str | None = None,
    category: str | None = None,
    sort: str = "relevance",
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        return await _provider(source).search(q, game_version, loader, category, sort, offset, limit)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{source} request failed: {exc}") from exc


@router.get("/filters", response_model=FilterOptions)
async def filters(source: Source = "modrinth"):
    try:
        return await _provider(source).filter_options()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{source} request failed: {exc}") from exc


@router.get("/packs/{source}/{project_id}/versions", response_model=list[PackVersion])
async def versions(source: Source, project_id: str):
    try:
        return await _provider(source).get_versions(project_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{source} request failed: {exc}") from exc
