"""FastAPI entry point. Routers are mounted under /api; the built web UI is served from /."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import APP_VERSION, settings
from .providers import ProviderError
from .routers import backups, cf_import, cloudflare, custom_packs, mod_db, search, server_admin, servers, similar
from .routers import settings as settings_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Scheduled world backups. BACKUP_SCHEDULER=false turns them off (e.g. for a dev copy).
    scheduler = None
    if os.environ.get("BACKUP_SCHEDULER", "true").lower() not in ("0", "false", "no"):
        scheduler = backups.get_backups()
        scheduler.start_scheduler()
    # Keeps tunnel ports and the public IP for direct-mode DNS records up to date.
    cf_sync = None
    if os.environ.get("CLOUDFLARE_SYNC", "true").lower() not in ("0", "false", "no"):
        from .routers.servers import get_manager
        from .services.cloudflare import SyncLoop

        cf_sync = SyncLoop(get_manager)
        cf_sync.start()
    yield
    if scheduler is not None:
        scheduler.stop_scheduler()
    if cf_sync is not None:
        cf_sync.stop()


app = FastAPI(title="Minecraft Server Manager", version=APP_VERSION, lifespan=lifespan)

app.include_router(search.router)
app.include_router(settings_router.router)
app.include_router(servers.router)
app.include_router(server_admin.router)
app.include_router(mod_db.router)
app.include_router(custom_packs.router)
app.include_router(similar.router)
app.include_router(backups.router)
app.include_router(cloudflare.router)
app.include_router(cf_import.router)


@app.exception_handler(ProviderError)
async def provider_error(_: Request, exc: ProviderError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


static = Path(settings.static_dir)
if static.is_dir():
    app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        file = static / path
        if path and file.is_file():
            return FileResponse(file)
        return FileResponse(static / "index.html")
