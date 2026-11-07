"""Create, list and control Minecraft server containers."""

from functools import lru_cache
from typing import Annotated

from docker.errors import DockerException
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from ..config import settings
from ..models.server import BuildInfo, CreateServerRequest, ServerInfo
from ..services.docker_manager import DockerManager, ServerConflict, ServerNotFound
from ..services.itzg_env import EnvBuildError

router = APIRouter(prefix="/api/servers", tags=["servers"])


@lru_cache
def get_manager() -> DockerManager:
    return DockerManager(settings)


def _call(fn, *args, **kwargs):
    """Run a manager call and map its errors to HTTP responses."""
    try:
        return fn(*args, **kwargs)
    except ServerNotFound as e:
        raise HTTPException(404, f"Server '{e}' not found") from e
    except ServerConflict as e:
        raise HTTPException(409, str(e)) from e
    except EnvBuildError as e:
        raise HTTPException(400, str(e)) from e
    except DockerException as e:
        msg = getattr(e, "explanation", None) or str(e)
        raise HTTPException(502, f"Docker error: {msg}") from e


Manager = Annotated[DockerManager, Depends(get_manager)]

# These handlers are plain `def` so FastAPI runs the blocking Docker calls in a threadpool.


@router.get("", response_model=list[ServerInfo])
def list_servers(mgr: Manager):
    return _call(mgr.list_servers)


@router.post("", response_model=ServerInfo, status_code=202)
def create_server(req: CreateServerRequest, mgr: Manager):
    from .cloudflare import on_server_created

    info = _call(mgr.create_server, req)
    on_server_created(info.id, info.port)
    return info


@router.get("/{server_id}", response_model=ServerInfo)
def get_server(server_id: str, mgr: Manager):
    return _call(mgr.get_server, server_id)


@router.post("/{server_id}/start", response_model=ServerInfo)
def start_server(server_id: str, mgr: Manager):
    return _call(mgr.start_server, server_id)


@router.post("/{server_id}/stop", response_model=ServerInfo)
def stop_server(server_id: str, mgr: Manager):
    return _call(mgr.stop_server, server_id)


@router.post("/{server_id}/restart", response_model=ServerInfo)
def restart_server(server_id: str, mgr: Manager):
    return _call(mgr.restart_server, server_id)


@router.post("/{server_id}/recreate", response_model=ServerInfo, status_code=202)
def recreate_server(server_id: str, mgr: Manager):
    """Make a new container for one of our servers whose container was deleted or lost."""
    return _call(mgr.recreate_server, server_id)


@router.delete("/{server_id}", status_code=204)
def delete_server(
    server_id: str,
    mgr: Manager,
    delete_data: Annotated[bool, Query(description="Also delete the world and server files")] = False,
):
    from .cloudflare import on_server_deleted

    _call(mgr.delete_server, server_id, delete_data=delete_data)
    on_server_deleted(server_id)


@router.get("/{server_id}/logs", response_class=PlainTextResponse)
def server_logs(server_id: str, mgr: Manager, tail: Annotated[int, Query(ge=1, le=5000)] = 200):
    return _call(mgr.get_logs, server_id, tail=tail)


@router.get("/{server_id}/build", response_model=BuildInfo | None)
def server_build(server_id: str, mgr: Manager):
    """The automatic install-and-test build report, if the server had one."""
    _call(mgr.get_server, server_id)
    return mgr.get_build(server_id)
