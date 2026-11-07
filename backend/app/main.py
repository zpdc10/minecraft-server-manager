"""FastAPI entry point. Routers are mounted under /api; the built web UI is served from /."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import APP_VERSION, settings
from .routers import search

app = FastAPI(title="Minecraft Server Manager", version=APP_VERSION)

app.include_router(search.router)
# Step 2 adds: app.include_router(servers.router)


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
