# Minecraft Server Manager

A Docker app for Unraid with a web UI to search Modrinth and CurseForge for modpacks and spin each one up as its own Minecraft server container.

Servers run on the [`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server) image, which installs modpacks from a pack ID. This app handles search, settings and container management.

## Status

- [x] Modrinth modpack search with version, loader and category filters
- [ ] Create and manage servers (port, RAM, start/stop/delete)
- [x] CurseForge search (needs an API key, set in Settings or via `CF_API_KEY`)
- [ ] Console, backups, whitelist, remote access

## Install on Unraid

1. Copy `unraid/minecraft-server-manager.xml` to `/boot/config/plugins/dockerMan/templates-user/` on your Unraid server.
2. In the Docker tab, click **Add Container** and choose **minecraft-server-manager** from the template list.
3. Apply, then open the web UI at `http://<unraid-ip>:8080`.

The image `ghcr.io/zpdc10/minecraft-server-manager:latest` is built by GitHub Actions on every push to `main`. If the package is private, make it public in the repo's Packages settings so Unraid can pull it.

## CurseForge

CurseForge requires an API key for search and for installing packs. Apply for a free key at
[console.curseforge.com](https://console.curseforge.com) (approval can take a few days), then paste it on the
**Settings** page, or set the `CF_API_KEY` container variable. Until a key is set, the CurseForge source shows a
message pointing to Settings and Modrinth keeps working.

Some CurseForge packs have mods whose authors block third-party downloads. The itzg server image then can't fetch
those mods automatically and will list the files it needs you to download by hand.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CF_API_KEY` | (empty) | CurseForge API key. A key saved on the Settings page overrides this. |
| `DATA_DIR` | `/data` | Where the app keeps `settings.json` (UI-saved settings, including the CurseForge key) |
| `MODRINTH_API_URL` | `https://api.modrinth.com/v2` | Override the Modrinth API base URL |

## Project layout

```
backend/app/
  main.py            FastAPI app; mounts routers under /api and serves the UI
  config.py          settings from environment variables
  models/            shared data models (modpack.py, ...)
  providers/         modpack sources; each implements providers/base.py
  routers/           API endpoints (search.py, ...)
backend/tests/       pytest suite (HTTP mocked with respx)
frontend/src/
  api.js             API client
  pages/             top-level pages (Search.jsx, ...)
  components/        shared UI pieces
unraid/              Unraid Docker template
Dockerfile           multi-stage build: UI, then Python runtime
```

## API

| Endpoint | Description |
| --- | --- |
| `GET /api/sources` | Available modpack sources |
| `GET /api/filters?source=` | Minecraft versions, loaders and categories for the filters |
| `GET /api/search?source=&q=&game_version=&loader=&category=&sort=&offset=&limit=` | Search modpacks |
| `GET /api/packs/{source}/{id}/versions` | Versions of one pack |
| `GET /api/settings` | App settings (CurseForge key status; the key itself is never returned) |
| `PUT /api/settings/curseforge` | Save (`{"api_key": "..."}`, checked against CurseForge first) or clear (`""`) the CurseForge key |
| `POST /api/settings/curseforge/test` | Check the current CurseForge key |
| `GET /api/health` | Health check |

## Development

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest

# frontend (proxies /api to :8000)
cd frontend
npm install
npm run dev
```

Build the image locally with `docker build -t minecraft-server-manager .`
