"""App settings editable from the web UI: currently the CurseForge API key."""

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import settings_store
from ..providers import curseforge

router = APIRouter(prefix="/api/settings", tags=["settings"])


class CurseForgeStatus(BaseModel):
    configured: bool
    # "settings" = saved from the UI, "env" = CF_API_KEY env var, None = no key.
    key_source: str | None
    # Last 4 characters only; the key itself is never sent back to the browser.
    key_hint: str | None
    verified: bool | None = None


class AppSettings(BaseModel):
    curseforge: CurseForgeStatus


class CurseForgeKeyUpdate(BaseModel):
    # Empty string removes the saved key (falls back to CF_API_KEY if set).
    api_key: str


def _status(verified: bool | None = None) -> CurseForgeStatus:
    key = settings_store.curseforge_api_key()
    return CurseForgeStatus(
        configured=bool(key),
        key_source=settings_store.curseforge_key_source(),
        key_hint=f"…{key[-4:]}" if len(key) >= 8 else None,
        verified=verified,
    )


@router.get("", response_model=AppSettings)
async def get_settings():
    return AppSettings(curseforge=_status())


@router.put("/curseforge", response_model=CurseForgeStatus)
async def set_curseforge_key(body: CurseForgeKeyUpdate):
    key = body.api_key.strip()
    if not key:
        settings_store.save({"curseforge_api_key": None})
        curseforge._cache.clear()
        return _status()
    try:
        ok = await curseforge.check_api_key(key)
    except httpx.HTTPError:
        ok = None  # CurseForge unreachable: save anyway, report as unverified
    if ok is False:
        raise HTTPException(status_code=400, detail="CurseForge rejected this API key, so it was not saved.")
    settings_store.save({"curseforge_api_key": key})
    curseforge._cache.clear()
    return _status(verified=ok)


@router.post("/curseforge/test", response_model=CurseForgeStatus)
async def test_curseforge_key():
    key = settings_store.curseforge_api_key()
    if not key:
        return _status(verified=False)
    try:
        return _status(verified=await curseforge.check_api_key(key))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach CurseForge: {exc}") from exc
