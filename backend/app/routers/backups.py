"""World backups: list, take, restore, download and delete, plus schedules and the backup folder."""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..services.backups import (
    BackupError,
    BackupJob,
    BackupLocation,
    BackupManager,
    BackupNotFound,
    BackupSchedule,
    ServerBackups,
)
from .servers import _call, get_manager

router = APIRouter(tags=["backups"])


@lru_cache
def get_backups() -> BackupManager:
    return BackupManager(get_manager(), settings)


Backups = Annotated[BackupManager, Depends(get_backups)]


def _run(fn, *args, **kwargs):
    try:
        return _call(fn, *args, **kwargs)
    except BackupNotFound as e:
        raise HTTPException(404, f"Backup '{e}' not found") from e
    except BackupError as e:
        raise HTTPException(400, str(e)) from e


class BackupRequest(BaseModel):
    # world = world folders plus whitelist/ops/properties; full = the whole server folder.
    # Empty uses the server's schedule setting.
    scope: str = Field(default="", pattern="^(|world|full)$")


class LocationUpdate(BaseModel):
    # Folder inside the app container. Empty goes back to the default.
    path: str


@router.get("/api/servers/{server_id}/backups", response_model=ServerBackups)
def list_backups(server_id: str, b: Backups):
    return _run(b.overview, server_id)


@router.post("/api/servers/{server_id}/backups", response_model=BackupJob, status_code=202)
def create_backup(server_id: str, b: Backups, body: BackupRequest | None = None):
    scope = body.scope if body and body.scope else None
    return _run(b.start_backup, server_id, kind="manual", scope=scope)


@router.put("/api/servers/{server_id}/backups/schedule", response_model=BackupSchedule)
def set_schedule(server_id: str, schedule: BackupSchedule, b: Backups):
    return _run(b.set_schedule, server_id, schedule)


@router.get("/api/servers/{server_id}/backups/{backup_id}/download")
def download_backup(server_id: str, backup_id: str, b: Backups):
    path = _run(b.backup_path, server_id, backup_id)
    return FileResponse(path, media_type="application/zip", filename=backup_id)


@router.post("/api/servers/{server_id}/backups/{backup_id}/restore", response_model=BackupJob, status_code=202)
def restore_backup(server_id: str, backup_id: str, b: Backups):
    return _run(b.start_restore, server_id, backup_id)


@router.delete("/api/servers/{server_id}/backups/{backup_id}", status_code=204)
def delete_backup(server_id: str, backup_id: str, b: Backups):
    _run(b.delete_backup, server_id, backup_id)


@router.get("/api/backups/location", response_model=BackupLocation)
def get_location(b: Backups):
    return b.location()


@router.put("/api/backups/location", response_model=BackupLocation)
def set_location(body: LocationUpdate, b: Backups):
    return _run(b.set_location, body.path)
