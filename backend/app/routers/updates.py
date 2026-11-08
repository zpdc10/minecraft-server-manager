"""Modpack updates: check servers for newer pack versions, link imported packs, update safely."""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from ..models.updates import LinkUpdate, ServerUpdates, UpdateJob, UpdateRequest
from ..services.pack_updates import UpdateError, UpdateManager
from .backups import get_backups
from .servers import _call, get_manager

router = APIRouter(tags=["updates"])


@lru_cache
def get_updates() -> UpdateManager:
    return UpdateManager(get_manager(), get_backups())


Updates = Annotated[UpdateManager, Depends(get_updates)]


def _run(fn, *args, **kwargs):
    try:
        return _call(fn, *args, **kwargs)
    except UpdateError as e:
        raise HTTPException(400, str(e)) from e


async def _arun(coro):
    try:
        return await coro
    except UpdateError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/updates", response_model=list[ServerUpdates])
async def check_all(updates: Updates, refresh: Annotated[bool, Query()] = False):
    """Every modpack server and whether a newer pack version is out (cached for an hour)."""
    return await _arun(updates.check_all(refresh=refresh))


@router.get("/api/servers/{server_id}/updates", response_model=ServerUpdates)
async def server_updates(server_id: str, updates: Updates, refresh: Annotated[bool, Query()] = False):
    await run_in_threadpool(_run, updates.mgr.get_server, server_id)
    await _arun(updates.check(server_id, refresh=refresh))
    return await run_in_threadpool(_run, updates.overview, server_id)


@router.put("/api/servers/{server_id}/updates/link", response_model=ServerUpdates)
async def link_pack(server_id: str, body: LinkUpdate, updates: Updates):
    """Tell the app which CurseForge pack an imported server runs, so it can be checked for updates."""
    await run_in_threadpool(_run, updates.mgr.get_server, server_id)
    await _arun(updates.link_pack(server_id, body.url))
    return await run_in_threadpool(_run, updates.overview, server_id)


@router.post("/api/servers/{server_id}/updates", response_model=UpdateJob, status_code=202)
async def start_update(server_id: str, body: UpdateRequest, updates: Updates):
    """Back up, install the version, test-start it, and roll back if it doesn't start."""
    await run_in_threadpool(_run, updates.mgr.get_server, server_id)
    check = await _arun(updates.check(server_id))
    if not any(v.id == body.version_id for v in check.newer):
        check = await _arun(updates.check(server_id, refresh=True))
    return await run_in_threadpool(_run, updates.start_update, server_id, check, body.version_id)
