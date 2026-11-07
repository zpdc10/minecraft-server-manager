"""FastAPI entry point. Routers are mounted under /api; the built web UI is served from /."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import APP_VERSION, settings
from .providers import ProviderError
from .routers import search, servers
from .routers import settings as settings_router

app = FastAPI(title="Minecraft Server Manager", version=APP_VERSION)

app.include_router(search.router)
app.include_router(settings_router.router)
app.include_router(servers.router)


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
