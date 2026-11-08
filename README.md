# Minecraft Server Manager

A Docker app for Unraid with a web UI to search Modrinth and CurseForge for modpacks and spin each one up as its own Minecraft server container.

Servers run on the [`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server) image, which installs modpacks from a pack ID. This app handles search, settings and container management.

## Status

- [x] Modrinth modpack search with version, loader and category filters
- [x] Create and manage servers (port, RAM, Java version, start/stop/restart/delete, logs)
- [x] CurseForge search and installs (needs an API key, set in Settings or via `CF_API_KEY`)
- [x] Build servers from client-only packs: install, test-start, and remove client-only or crashing mods automatically
- [x] Pack builder: assemble a custom pack from Modrinth and CurseForge mods, run it as a server, export it for launchers
- [x] World backups: manual and scheduled, with retention, restore, download and a configurable backup folder
- [x] Modpack updates: check every server for a newer pack version, then back up, update, test-start and roll back if it fails
- [ ] Console, whitelist, remote access

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

## Building servers from client packs

Many packs have no server download, or ship client-only mods that crash a dedicated server. When
"Test and fix automatically" is ticked (the default for modpacks, and always on for packs marked client-only),
the app builds the server in steps:

1. **Install.** The itzg container runs once with `SETUP_ONLY=true` to download the pack, loader and mods.
   itzg already skips files a Modrinth pack marks as not needed on servers, and applies its own exclude lists.
2. **Scan.** Every jar in `mods/` is opened. Jars whose metadata says they only run on the client
   (`"environment": "client"` in fabric.mod.json, `clientSideOnly=true` in mods.toml), and jars on the
   known-mods list, are set aside.
3. **Test.** The server is started with auto-restart off and the log is watched for `Done (...)! For help`.
   If it crashes, the log and new crash reports are read to find the mod responsible: a mod loading client
   classes on the server, a mod missing a dependency (often one removed in an earlier step), a mod the loader
   reports as failed, a broken mixin, or the first mod in the crash's stack trace. That mod is set aside and
   the server is tried again, up to `BUILD_MAX_ATTEMPTS` times. If the mods need a newer Java, the image is
   switched instead. Out-of-memory and port-in-use errors stop the build with a message.

Removed jars are moved to `.mcsm/removed-mods/` in the server folder, so they can be put back by hand, and are
passed to itzg as excluded / ignored files so later restarts don't download them again. The Servers page lists
each removed mod and why. The full report is in `.mcsm/build.json` and at `GET /api/servers/<id>/build`.

### Known client-only mods list

`backend/app/data/known-mods.json` is a shared list of mods known to be client-only or to break servers,
seeded from itzg's exclude lists. The app downloads the latest copy from `MOD_DB_URL` (this repo on GitHub)
every few hours and falls back to the copy built into the image. Mods a build removes for loading client-only
code are also remembered on that install (`learned-mods.json` in `DATA_DIR`) and removed up front from later
builds; they are listed on the Settings page, where they can be forgotten. Copy entries from there into
`known-mods.json` to share them with every install.

## Backups

Each server card has a **Backups** button. From there you can back up now, restore, download or delete a backup,
and turn on scheduled backups (every 30 minutes to every week) with retention: keep the newest N automatic backups
and optionally delete ones older than N days. Manual backups are never deleted automatically.

- **What is saved.** "World only" (the default) saves the world folders (`world`, `world_nether`, `world_the_end`
  and any other folder with a `level.dat`) plus `server.properties`, the whitelist, ops and ban lists.
  "Whole server folder" saves everything except logs and crash reports.
- **Consistency.** While a running server is backed up, the app pauses world saving with RCON
  (`save-off`, `save-all flush`), copies the files, then turns saving back on. If RCON isn't reachable the backup is
  still taken and marked as possibly inconsistent.
- **Restore.** The server is stopped, a "before restore" safety backup of the current world is taken, the backup is
  unpacked into a staging folder and swapped in, and the server is started again if it was running.
- **Format.** Backups are plain zip files named `<server>_<UTC time>_<kind>.zip`, so they can be opened or restored
  by hand. Scheduled backups of a stopped server that hasn't changed are skipped.
- **Where.** `/backups` if it is mapped into the container (the Unraid template maps it, defaulting to
  `appdata/minecraft-server-manager/backups`; point it at a share to keep backups off the cache drive), otherwise
  `$DATA_DIR/backups`. The folder can also be changed on the Settings page. Backups stay when a server is deleted.

## Modpack updates

The Servers page checks every Modrinth, CurseForge and imported CurseForge server for a newer version of its pack
(results are cached for an hour; **Check for updates** asks again). A server with a newer version gets an
**Update available** badge and an **Update** button.

- **What counts as newer.** Versions released after the one the server runs, for the same loader. The suggested one
  is the newest release for the same Minecraft version; betas and versions that move to another Minecraft version
  can still be picked, with a warning.
- **Imported CurseForge packs.** The pack's CurseForge link is saved in the server folder (`.mcsm/updates.json`)
  when the server is made, so it can be checked again without an API key. A pack uploaded as a zip has no link: the
  app looks for it on CurseForge by name, and the Updates dialog lets you paste the right link.
- **Updating.** The server is stopped, the whole server folder is backed up (a "before update" backup), the new
  version is installed (imported packs get their old mods cleared and the new ones downloaded), and the server is
  test-started with the same auto-fix as the first build. If it starts, it stays on the new version (and is left
  stopped if it was stopped). If it doesn't, the backup is restored and the old container put back, so nothing
  changes; the failed test report stays in the update history.

## Public addresses with Cloudflare

Settings › **Cloudflare** links the app to your Cloudflare account with an API token (permissions:
Account › Cloudflare Tunnel › Edit, Zone › DNS › Edit, Zone › Zone › Read, limited to your domain). The app then
gives each new server an address such as `survival.example.com` (turn this off to add them by hand on the server's
**Public address** tab), updates it when the server's port changes, and deletes it when the server is deleted.

- **Its own tunnel.** The app makes a tunnel named `minecraft-server-manager` and runs a `mcsm_cloudflared`
  container for it (host networking, so it reaches every server at `localhost:<port>`). Tunnels, connectors and DNS
  records you already have are never edited; DNS records the app makes carry the comment
  "Managed by Minecraft Server Manager", and it only deletes records it made.
- **Tunnel mode (default).** A proxied CNAME to the tunnel plus a `tcp://localhost:<port>` route. Nothing is opened on
  the router and the home IP stays hidden. Minecraft Java speaks raw TCP, which a Cloudflare tunnel only carries to
  clients running cloudflared, so each player installs it once and runs
  `cloudflared access tcp --hostname survival.example.com --url 127.0.0.1:25565`, then joins `127.0.0.1:25565`.
  The only way around that on Cloudflare is Spectrum, a paid add-on.
- **playit.gg mode (players install nothing, free).** A free Cloudflare tunnel can't take Minecraft connections
  from the game itself, so for a free, nothing-to-install setup the app uses [playit.gg](https://playit.gg)
  (free for 4 tunnels). Paste a playit Docker agent's secret key in Settings and the app runs it as the `mcsm_playit`
  container (host networking). For each server, add a "Minecraft Java" tunnel in the playit dashboard pointing at
  `127.0.0.1:<server port>` and paste the address it gives (`abc.joinmc.link`) on the server's Public address tab.
  The app looks up where that address really leads (playit's names are SRV records, and Minecraft follows only one)
  and makes a DNS-only record plus a `_minecraft._tcp` SRV record, so players join `survival.example.com`. The
  10-minute sync re-points the records if playit moves the tunnel.
- **Direct mode.** A DNS-only A record to your public IP (detected and re-checked every 10 minutes, or a fixed IP or
  hostname you enter) plus a `_minecraft._tcp` SRV record with the server's port. Players type the address into
  Minecraft with nothing to install, but you must forward the port on your router and the address reveals your IP.
- **Names** are one level below the domain (`name.example.com`), because Cloudflare's free certificate doesn't cover
  deeper names. **Disconnect** removes every address the app made, its tunnel and its container.
- The manager's own web UI is not published. If you put it on your existing tunnel, protect it with Cloudflare
  Access, since the app has no login.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CF_API_KEY` | (empty) | CurseForge API key. A key saved on the Settings page overrides this. |
| `DATA_DIR` | `/data` | Where the app keeps `settings.json` (UI-saved settings, including the CurseForge key) |
| `DATA_ROOT` | `/data/servers` | Folder (inside this container) that holds one subfolder per server |
| `HOST_DATA_ROOT` | auto-detected | The same folder as a host path, used for the server containers' bind mounts |
| `DOCKER_NETWORK` | (empty) | Docker network to attach server containers to |
| `MC_UID` / `MC_GID` | `99` / `100` | User and group the Minecraft servers run as (Unraid's nobody:users) |
| `MOD_DB_URL` | this repo's `known-mods.json` on GitHub | Shared client-only mods list. Empty uses only the copy in the image |
| `BUILD_MAX_ATTEMPTS` | `10` | Test starts the server builder tries before giving up |
| `BUILD_MAX_REMOVALS` | `15` | Most mods the builder removes for crashes in one build |
| `BUILD_INSTALL_TIMEOUT` / `BUILD_START_TIMEOUT` | `3600` / `1200` | Seconds to wait for the pack install / for the server to start |
| `BACKUP_DIR` | `/backups` if mapped, else `$DATA_DIR/backups` | Backup folder. A folder chosen on the Settings page overrides this |
| `BACKUP_FLUSH_WAIT` | `3` | Seconds to wait after `save-all flush` before copying the world |
| `BACKUP_SCHEDULER` | `true` | Set to `false` to turn scheduled backups off for this install |
| `CLOUDFLARE_API_TOKEN` | (empty) | Cloudflare API token. A token saved on the Settings page overrides this |
| `CLOUDFLARE_SYNC` | `true` | Set to `false` to stop the 10-minute check of tunnel ports and the public IP |
| `PLAYIT_IMAGE` | `playitcloud/playit:0.15.19` | Image for the playit.gg agent |
| `CLOUDFLARED_IMAGE` | `cloudflare/cloudflared:latest` | Image for the app's tunnel connector |
| `MODRINTH_API_URL` | `https://api.modrinth.com/v2` | Override the Modrinth API base URL |

## How servers work

Each server is its own `itzg/minecraft-server` container named `mcsm-<server-id>`, with
`restart: unless-stopped`. Its files live in `/mnt/user/appdata/minecraft-server-manager/servers/<server-id>`
on the host. The server's settings (pack, version, port, RAM) are stored as container labels, with
a copy in the server folder at `.mcsm/server.json`. If a server's container is deleted or lost (for
example after Unraid's docker image is rebuilt), the server still shows up as "Container missing"
with a **Recreate container** button that rebuilds it from that copy, using the same files.

### Servers the app didn't create

The Servers page also lists every other Minecraft container on the host, marked "Found on host".
It finds them by image (itzg/minecraft-server, Paper, Purpur, Forge, Fabric, Bedrock and so on), by
`EULA=TRUE` in their environment, or by port 25565/19132. You can start, stop and restart them, read
their logs, use the console (itzg images) and see their stats. The app never recreates, renames or
deletes these containers, and their settings stay in Unraid's Docker tab.

Backups, mods, players lists and server.properties need the app to see the server's files. That
works when the server's data folder is inside a path mapped into the manager. Add a path in the
manager's Unraid template that maps, for example, `/mnt/user/appdata` to the same path
`/mnt/user/appdata`, and the app finds the files on its own.

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
  services/          docker_manager.py (server containers), itzg_env.py (image env vars),
                     server_builder.py (install/test/fix loop), crash_analyzer.py, mod_scan.py, mod_db.py
  data/known-mods.json  shared list of client-only mods
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
| `DELETE /api/servers/{id}?delete_data=false` | Remove a server, optionally with its files (not allowed for servers found on the host) |
| `POST /api/servers/{id}/recreate` | Rebuild the container of a server whose container is missing |
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
| `GET /api/cloudflare` · `DELETE` | Cloudflare link status / disconnect and remove everything the app made |
| `PUT /api/cloudflare/token` | Save a token `{api_token}`; returns the domains it can see |
| `PUT /api/cloudflare/playit` | Run the playit.gg agent with `{secret_key}` (empty removes it) |
| `PUT /api/cloudflare/config` | `{zone_id, auto_route, default_mode: tunnel\|direct\|playit, direct_target}`; sets up the tunnel and connector |
| `POST /api/cloudflare/sync` | Repair the connector and bring ports and IPs up to date |
| `GET /api/cloudflare/routes` | Public addresses with player instructions |
| `PUT /api/cloudflare/routes/{server_id}` · `DELETE` | Create or change `{subdomain, mode, playit_address}` / remove a server's address |
| `GET /api/updates?refresh=false` | Every modpack server and whether a newer pack version is out |
| `GET /api/servers/{id}/updates?refresh=false` | One server's update check, running update and past updates |
| `PUT /api/servers/{id}/updates/link` | Set the CurseForge link of an imported pack `{url}` |
| `POST /api/servers/{id}/updates` | Back up, update to `{version_id}`, test-start, roll back on failure |
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
