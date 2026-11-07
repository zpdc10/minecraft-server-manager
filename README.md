# Minecraft Server Manager

A Docker app for Unraid with a web UI to search Modrinth and CurseForge for modpacks and spin each one up as its own Minecraft server container.

Servers run on the [`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server) image, which installs modpacks from a pack ID. This app handles search, settings and container management.

## Status

- [x] Modrinth modpack search with version, loader and category filters
- [x] Create and manage servers (port, RAM, Java version, start/stop/restart/delete, logs)
- [x] CurseForge search and installs (needs an API key, set in Settings or via `CF_API_KEY`)
- [x] Pack builder: assemble a custom pack from Modrinth and CurseForge mods, run it as a server, export it for launchers
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
| `DATA_ROOT` | `/data/servers` | Folder (inside this container) that holds one subfolder per server |
| `HOST_DATA_ROOT` | auto-detected | The same folder as a host path, used for the server containers' bind mounts |
| `DOCKER_NETWORK` | (empty) | Docker network to attach server containers to |
| `MC_UID` / `MC_GID` | `99` / `100` | User and group the Minecraft servers run as (Unraid's nobody:users) |
| `MODRINTH_API_URL` | `https://api.modrinth.com/v2` | Override the Modrinth API base URL |

## How servers work

Each server is its own `itzg/minecraft-server` container named `mcsm-<server-id>`, with
`restart: unless-stopped`. Its files live in `/mnt/user/appdata/minecraft-server-manager/servers/<server-id>`
on the host. The server's settings (pack, version, port, RAM) are stored as container labels, so
there is no separate database. The app only ever touches containers labelled `mcsm.managed=true`.

The Java version is picked from the pack's Minecraft version (1.16 and older use Java 8,
1.17 Java 16, 1.18 to 1.20.4 Java 17, newer Java 21) and can be overridden when creating.
RAM sets the Java heap; the container memory limit adds 25% (at least 1 GB) of headroom.
Deleting a server keeps its files unless you tick "also delete files".

## Pack builder

"Build a pack" lets you make your own modpack. Pick a Minecraft version and loader
(Fabric, Forge, NeoForge or Quilt), then search Modrinth and CurseForge and add mods.

- Each mod is added at its newest stable version for the pack's Minecraft version and loader.
  Required dependencies are added too, recursively, and reused if they're already in the pack
  (also across sites, matched by slug). Removing a mod removes dependencies only it needed.
- The pack shows problems: mods that don't fit the version or loader, missing dependencies,
  mods marked incompatible with each other, and the same mod added twice.
- Changing the Minecraft version or loader re-picks a version of every mod.
- **Create server** copies the server-side mods into the server's `mods` folder (client-only
  mods are skipped) and runs itzg with `TYPE=FABRIC`/`FORGE`/`NEOFORGE`/`QUILT` and the
  pack's loader version.
- **Export .mrpack** makes a Modrinth pack (Modrinth App, Prism, GDLauncher, ATLauncher).
  **Export CurseForge zip** makes a CurseForge pack (CurseForge app, GDLauncher, Prism).
  Mods from the other site are matched to the same file by hash where possible (CurseForge
  matching needs the API key); otherwise their jar is bundled in the pack's `overrides/mods`.
  CurseForge mods whose authors block outside downloads only make it into the CurseForge zip.

Packs are saved as JSON in `/data/packs`.

## Project layout

```
backend/app/
  main.py            FastAPI app; mounts routers under /api and serves the UI
  config.py          settings from environment variables
  models/            shared data models (modpack.py, ...)
  providers/         modpack sources; each implements providers/base.py
  routers/           API endpoints (search.py, servers.py, ...)
  services/          docker_manager.py (server containers), itzg_env.py (image env vars)
backend/tests/       pytest suite (HTTP mocked with respx)
frontend/src/
  api.js             API client
  api/servers.js     server management API client
  pages/             top-level pages (Search.jsx, Servers.jsx, ...)
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
| `GET /api/servers` | List servers with status |
| `POST /api/servers` | Create a server (`source` `custom` with the pack id as `project_id` for a built pack): `{name, source, project_id, project_slug, version_id, minecraft_version, port, memory_gb, java_version?}` |
| `GET /api/servers/{id}` | One server |
| `POST /api/servers/{id}/start` · `/stop` · `/restart` | Control a server |
| `DELETE /api/servers/{id}?delete_data=false` | Remove a server, optionally with its files |
| `GET /api/servers/{id}/logs?tail=200` | Recent console output |
| `GET /api/mods/search?source=&q=&game_version=&loader=&category=&sort=&offset=&limit=` | Search mods |
| `GET /api/mods/filters?source=` | Mod categories and Minecraft versions |
| `GET /api/mods/{source}/{id}/versions?game_version=&loader=` | Files of one mod |
| `GET /api/loaders/{loader}/versions?minecraft_version=` | Loader versions, recommended first |
| `GET /api/custom-packs` · `POST` | List packs / create `{name, minecraft_version, loader, loader_version?}` |
| `GET /api/custom-packs/{id}` · `PUT` · `DELETE` | One pack with its problems / edit (changing version or loader re-picks mods) / delete |
| `POST /api/custom-packs/{id}/mods` | Add a mod and its dependencies: `{source, project_id, version_id?}` |
| `DELETE /api/custom-packs/{id}/mods/{source}/{project_id}` | Remove a mod and dependencies only it needed |
| `GET /api/custom-packs/{id}/export/mrpack` · `/curseforge` | Download the pack; warnings in the `X-Pack-Warnings` header |
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
