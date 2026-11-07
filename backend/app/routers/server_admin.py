"""Management tools for one server: console, players, server.properties, settings, stats, mods."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from ..models.admin import (
    ConsoleRequest,
    ConsoleResult,
    ModFile,
    PlayerActionRequest,
    PlayerLists,
    PropertiesInfo,
    PropertiesUpdate,
    ServerStats,
    UpdateSettingsRequest,
    WhitelistToggle,
)
from ..models.server import ServerInfo
from ..services.server_admin import MAX_UPLOAD_BYTES, AdminError, ServerAdmin
from .servers import Manager, _call

router = APIRouter(prefix="/api/servers/{server_id}", tags=["server admin"])

# Plain `def` handlers: FastAPI runs the blocking Docker calls in a threadpool.


def _admin(fn_name: str, mgr, *args, **kwargs):
    admin = ServerAdmin(mgr)
    try:
        return _call(getattr(admin, fn_name), *args, **kwargs)
    except AdminError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/console", response_model=ConsoleResult)
def console(server_id: str, req: ConsoleRequest, mgr: Manager):
    return _admin("console", mgr, server_id, req.command)


@router.get("/players", response_model=PlayerLists)
def players(server_id: str, mgr: Manager):
    return _admin("players", mgr, server_id)


@router.post("/players", response_model=ConsoleResult)
def player_action(server_id: str, req: PlayerActionRequest, mgr: Manager):
    return _admin("player_action", mgr, server_id, req.action, req.name, req.reason)


@router.put("/whitelist", response_model=ConsoleResult)
def set_whitelist(server_id: str, req: WhitelistToggle, mgr: Manager):
    return _admin("set_whitelist", mgr, server_id, req.enabled)


@router.get("/properties", response_model=PropertiesInfo)
def get_properties(server_id: str, mgr: Manager):
    return _admin("properties", mgr, server_id)


@router.put("/properties", response_model=PropertiesInfo)
def save_properties(server_id: str, req: PropertiesUpdate, mgr: Manager):
    result = _admin("save_properties", mgr, server_id, req.properties)
    if req.restart and mgr.get_server(server_id).status in ("running", "starting"):
        _call(mgr.restart_server, server_id)
    return result


@router.patch("/settings", response_model=ServerInfo)
def update_settings(server_id: str, req: UpdateSettingsRequest, mgr: Manager):
    from .cloudflare import on_server_changed

    info = _admin("update_settings", mgr, server_id, req)
    on_server_changed(server_id, info.port)
    return info


@router.get("/stats", response_model=ServerStats)
def stats(server_id: str, mgr: Manager):
    return _admin("stats", mgr, server_id)


@router.get("/mods", response_model=list[ModFile])
def list_mods(server_id: str, mgr: Manager):
    return _admin("list_mods", mgr, server_id)


@router.post("/mods", response_model=list[ModFile])
async def upload_mod(server_id: str, request: Request, mgr: Manager, filename: Annotated[str, Query()]):
    """Upload a mod jar as the raw request body."""
    size = int(request.headers.get("content-length") or 0)
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "The file is too big")
    data = await request.body()
    return await run_in_threadpool(_admin, "upload_mod", mgr, server_id, filename, data)


@router.post("/mods/{filename}/enable", response_model=list[ModFile])
def enable_mod(server_id: str, filename: str, mgr: Manager):
    return _admin("set_mod_enabled", mgr, server_id, filename, True)


@router.post("/mods/{filename}/disable", response_model=list[ModFile])
def disable_mod(server_id: str, filename: str, mgr: Manager):
    return _admin("set_mod_enabled", mgr, server_id, filename, False)


@router.delete("/mods/{filename}", response_model=list[ModFile])
def delete_mod(server_id: str, filename: str, mgr: Manager):
    return _admin("delete_mod", mgr, server_id, filename)
