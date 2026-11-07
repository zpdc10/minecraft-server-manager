"""The known client-only / crashing mods list used by the server builder."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..services.mod_db import get_mod_db

router = APIRouter(prefix="/api/mod-db", tags=["mod-db"])


class ModDBStatus(BaseModel):
    url: str
    # remote = fetched from url, cache = last good fetch, bundled = the copy in the image
    source: str
    error: str = ""
    shared_count: int
    learned: list[dict]


@router.get("", response_model=ModDBStatus)
def mod_db_status():
    return get_mod_db(settings).status()


@router.delete("/learned/{mod_id}", status_code=204)
def forget_learned(mod_id: str):
    if not get_mod_db(settings).forget(mod_id):
        raise HTTPException(404, f"'{mod_id}' is not on the learned list")
