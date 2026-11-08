"""Check servers for newer modpack versions, and update them without risking the world.

Checking
- Modrinth and CurseForge (API key) servers: the pack's version list from the provider.
- Imported CurseForge packs (no API key): the pack's file list from cfwidget, using the
  CurseForge link saved when the server was made. Packs uploaded as a zip have no link,
  so the app tries to find the pack on CurseForge by its name, and the user can paste
  the right link instead. Either way the link is saved in the server folder
  (.mcsm/updates.json) so every later check uses it.
- The current version is the one the server was made from. When it was made from
  "latest", it is read from itzg's install manifest, or failing that every version
  published after the server was made counts as newer.

Updating (one background job per server)
1. Stop the server so the world is saved and consistent.
2. Take a full backup of the server folder ("pre-update").
3. Install the new version: point itzg at the new version (Modrinth / CurseForge), or
   clear the old mods and download the new pack files (imported packs).
4. Test-start it with the server builder, which removes client-only and crashing mods
   like it did when the server was first built.
5. If it started, keep it (left running only if it was running before). If not,
   restore the backup and put the old container back, so the server is exactly as it was.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import UTC, datetime

from docker.errors import DockerException

from .. import settings_store
from ..models.modpack import PackVersion
from ..models.server import BuildInfo, ServerInfo
from ..models.updates import PackLink, ServerUpdates, UpdateCheck, UpdateJob, UpdateVersion
from ..providers import PROVIDERS, ProviderError
from .docker_manager import DockerManager, ServerConflict
from .itzg_env import pick_java_version

log = logging.getLogger(__name__)

STATE_FILE = "updates.json"
CHECK_TTL = 3600  # seconds a check result is reused before asking the pack site again
HISTORY_KEEP = 10
CHECK_CONCURRENCY = 4
UPDATABLE_SOURCES = ("modrinth", "curseforge", "cfimport")
ACTIVE_PHASES = {"stopping", "backing-up", "installing", "testing", "rolling-back"}
# Env that says which loader to install. Imported packs get these again from the new manifest.
LOADER_KEYS = {
    "TYPE",
    "VERSION",
    "FORGE_VERSION",
    "NEOFORGE_VERSION",
    "FABRIC_LOADER_VERSION",
    "QUILT_LOADER_VERSION",
}
# itzg's own record of which pack version it installed.
ITZG_MANIFESTS = {
    "modrinth": (".modrinth-manifest.json", "versionId"),
    "curseforge": (".curseforge-manifest.json", "fileId"),
}


class UpdateError(Exception):
    """The update can't be started. The message is shown to the user."""


class _Unsupported(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def from_pack_version(v: PackVersion, info: ServerInfo) -> UpdateVersion:
    mcs = v.game_versions or []
    mc = info.minecraft_version if info.minecraft_version in mcs else (mcs[0] if mcs else "")
    loaders = [lo.lower() for lo in v.loaders or []]
    loader = info.loader.lower() if info.loader and info.loader.lower() in loaders else (loaders[0] if loaders else "")
    return UpdateVersion(
        id=v.id,
        name=v.name or v.version_number,
        minecraft_version=mc,
        loader=loader,
        published=v.published or "",
        release_type=v.release_type or "release",
    )


def pick_newer(versions: list[UpdateVersion], current_id: str, info: ServerInfo) -> list[UpdateVersion]:
    """Versions newer than the current one (versions are newest first)."""
    idx = next((i for i, v in enumerate(versions) if v.id == current_id), None) if current_id else None
    if idx is not None:
        newer = versions[:idx]
    else:
        # Made from "latest", or the version was taken down: go by when the server was made.
        made = _when(info.created_at)
        newer = [v for v in versions if made and (_when(v.published) or made) > made] if made else []
    if info.loader:
        newer = [v for v in newer if not v.loader or v.loader == info.loader.lower()]
    for v in newer:
        v.changes_minecraft = bool(
            info.minecraft_version and v.minecraft_version and v.minecraft_version != info.minecraft_version
        )
    return newer


def recommend(newer: list[UpdateVersion]) -> UpdateVersion | None:
    """The newest release for the same Minecraft version, else the newest version."""
    same = [v for v in newer if not v.changes_minecraft]
    for group in (same, newer):
        for v in group:
            if v.release_type == "release":
                return v
        if group:
            return group[0]
    return None


class UpdateManager:
    def __init__(self, mgr: DockerManager, backups):
        self.mgr = mgr
        self.backups = backups
        self._lock = threading.Lock()
        self._jobs: dict[str, UpdateJob] = {}
        self._checks: dict[str, tuple[UpdateCheck, float]] = {}

    # ---- saved state (.mcsm/updates.json) --------------------------------------

    def _state_path(self, server_id: str) -> str:
        return os.path.join(self.mgr.local_data_path(server_id), ".mcsm", STATE_FILE)

    def _load_state(self, server_id: str) -> dict:
        try:
            with open(self._state_path(server_id)) as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, server_id: str, state: dict) -> None:
        path = self._state_path(server_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=1)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("Could not save update state for %s: %s", server_id, e)

    def get_link(self, info: ServerInfo) -> PackLink | None:
        saved = self._load_state(info.id).get("link")
        if isinstance(saved, dict) and saved.get("project_id"):
            return PackLink(**saved)
        if info.source == "cfimport" and info.project_id.isdigit():
            slug = info.project_slug
            url = f"https://www.curseforge.com/minecraft/modpacks/{slug}" if slug else ""
            return PackLink(url=url, project_id=info.project_id, slug=slug, title=info.pack_title)
        return None

    def save_link(self, server_id: str, link: PackLink) -> None:
        state = self._load_state(server_id)
        state["link"] = link.model_dump()
        self._save_state(server_id, state)
        with self._lock:
            self._checks.pop(server_id, None)

    def remember_created(self, info: ServerInfo, pack_url: str) -> None:
        """Save where an imported CurseForge pack came from, when the server is made."""
        if info.source != "cfimport" or not info.project_id.isdigit():
            return
        self.save_link(
            info.id,
            PackLink(url=pack_url, project_id=info.project_id, slug=info.project_slug, title=info.pack_title),
        )

    def history(self, server_id: str) -> list[UpdateJob]:
        out = []
        for item in self._load_state(server_id).get("history") or []:
            try:
                out.append(UpdateJob(**item))
            except (TypeError, ValueError):
                pass
        return out

    # ---- checking ------------------------------------------------------------

    def cached_check(self, server_id: str) -> UpdateCheck | None:
        with self._lock:
            hit = self._checks.get(server_id)
        return hit[0] if hit else None

    async def check(self, server_id: str, refresh: bool = False) -> UpdateCheck:
        with self._lock:
            hit = self._checks.get(server_id)
        if hit and not refresh and time.monotonic() - hit[1] < CHECK_TTL:
            return hit[0]
        info = await asyncio.to_thread(self.mgr.get_server, server_id)
        result = await self._check(info)
        with self._lock:
            self._checks[server_id] = (result, time.monotonic())
        return result

    async def _check(self, info: ServerInfo) -> UpdateCheck:
        check = UpdateCheck(server_id=info.id, checked_at=_now())
        if not info.managed:
            return check.model_copy(update={"supported": False, "reason": "This server wasn't made by the app."})
        if info.source not in UPDATABLE_SOURCES:
            reason = (
                "Custom packs are changed in the pack builder."
                if info.source == "custom"
                else "This server doesn't run a modpack."
            )
            return check.model_copy(update={"supported": False, "reason": reason})
        try:
            versions, link, current_id = await self._versions(info)
        except _Unsupported as e:
            return check.model_copy(update={"supported": False, "reason": str(e), "link": self.get_link(info)})
        except ProviderError as e:
            return check.model_copy(update={"error": e.message, "link": self.get_link(info)})
        except Exception as e:  # noqa: BLE001 - network and site errors are shown, not raised
            log.warning("Update check of %s failed: %s", info.id, e)
            return check.model_copy(update={"error": f"Couldn't check for updates: {e}", "link": self.get_link(info)})
        current = next((v for v in versions if v.id == current_id), None)
        newer = pick_newer(versions, current_id, info)
        return check.model_copy(
            update={
                "current_version_id": current_id,
                "current_name": current.name if current else "",
                "newer": newer,
                "latest": recommend(newer),
                "update_available": bool(newer),
                "link": link,
            }
        )

    async def _versions(self, info: ServerInfo) -> tuple[list[UpdateVersion], PackLink | None, str]:
        """(all versions newest first, the CurseForge link used, the current version id)."""
        if info.source == "modrinth":
            pack = info.project_id or info.project_slug
            raw = await PROVIDERS["modrinth"].get_versions(pack)
            versions = [from_pack_version(v, info) for v in raw]
            versions.sort(key=lambda v: _when(v.published) or datetime.min.replace(tzinfo=UTC), reverse=True)
            return versions, None, info.version_id or await asyncio.to_thread(self._itzg_version, info)
        if info.source == "curseforge":
            if not settings_store.curseforge_api_key():
                raise _Unsupported("Add a CurseForge API key in Settings to check this pack for updates.")
            raw = await PROVIDERS["curseforge"].get_versions(info.project_id)
            versions = [from_pack_version(v, info) for v in raw]
            return versions, None, info.version_id or await asyncio.to_thread(self._itzg_version, info)

        # Imported CurseForge pack.
        from . import cf_import

        link = self.get_link(info)
        uploaded = info.project_id.startswith(cf_import.UPLOAD_PREFIX)
        manifest = await asyncio.to_thread(self._upload_manifest, info) if uploaded else None
        if link is None and uploaded:
            link = await self._guess_link(info, manifest)
            if link is None:
                raise _Unsupported(
                    "This pack was uploaded as a zip, so the app doesn't know which CurseForge pack it is. "
                    "Paste the pack's CurseForge link to check it for updates."
                )
            await asyncio.to_thread(self.save_link, info.id, link)
        preview = await cf_import.preview(link.project_id)
        versions = [
            UpdateVersion(
                id=f.id,
                name=f.display_name or f.name,
                minecraft_version=f.minecraft_version,
                loader=f.loader,
                published=f.uploaded_at,
                release_type=f.release_type or "release",
            )
            for f in preview.files
        ]
        if not link.url and preview.url:
            link = link.model_copy(update={"url": preview.url, "title": link.title or preview.title})
        current = info.version_id
        if uploaded or link.project_id != info.project_id:
            current = _match_upload(versions, manifest) if manifest else ""
        return versions, link, current

    def _itzg_version(self, info: ServerInfo) -> str:
        name, key = ITZG_MANIFESTS.get(info.source, ("", ""))
        if not name:
            return ""
        try:
            with open(os.path.join(self.mgr.local_data_path(info.id), name)) as f:
                return str(json.load(f).get(key) or "")
        except (OSError, ValueError, AttributeError):
            return ""

    def _upload_manifest(self, info: ServerInfo) -> dict | None:
        from . import cf_import

        try:
            return cf_import.read_manifest(cf_import.upload_path(info.project_id))
        except cf_import.PackImportError:
            return None

    async def _guess_link(self, info: ServerInfo, manifest: dict | None) -> PackLink | None:
        """Find an uploaded pack on CurseForge by its name."""
        from . import cf_import

        name = (manifest or {}).get("name") or info.pack_title
        slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
        if not slug:
            return None
        try:
            preview = await cf_import.preview(slug)
        except Exception:  # noqa: BLE001 - not found / site down: no guess
            return None
        if _norm(preview.title) != _norm(name):
            return None
        return PackLink(
            url=preview.url, project_id=preview.project_id, slug=preview.slug, title=preview.title, guessed=True
        )

    async def link_pack(self, server_id: str, url: str) -> UpdateCheck:
        """Save the CurseForge page an imported pack came from, then check it."""
        from . import cf_import

        info = await asyncio.to_thread(self.mgr.get_server, server_id)
        if info.source != "cfimport":
            raise UpdateError("Only imported CurseForge packs need a link; this one is checked on its own.")
        preview = await cf_import.preview(url)
        link = PackLink(url=preview.url, project_id=preview.project_id, slug=preview.slug, title=preview.title)
        await asyncio.to_thread(self.save_link, server_id, link)
        return await self.check(server_id, refresh=True)

    async def check_all(self, refresh: bool = False) -> list[ServerUpdates]:
        servers = await asyncio.to_thread(self.mgr.list_servers)
        servers = [s for s in servers if s.managed and s.source in UPDATABLE_SOURCES and s.status != "missing"]
        sem = asyncio.Semaphore(CHECK_CONCURRENCY)

        async def one(s: ServerInfo) -> ServerUpdates:
            async with sem:
                try:
                    check = await self.check(s.id, refresh=refresh)
                except Exception as e:  # noqa: BLE001
                    check = UpdateCheck(server_id=s.id, error=str(e), checked_at=_now())
            return ServerUpdates(server_id=s.id, check=check, job=self.job(s.id), link=check.link)

        return list(await asyncio.gather(*(one(s) for s in servers)))

    def job(self, server_id: str) -> UpdateJob | None:
        with self._lock:
            job = self._jobs.get(server_id)
        if job is None:
            return None
        build = self.mgr.get_build(server_id) if job.phase in ("installing", "testing") else job.build
        return job.model_copy(update={"build": build})

    def overview(self, server_id: str) -> ServerUpdates:
        info = self.mgr.get_server(server_id)
        check = self.cached_check(server_id)
        return ServerUpdates(
            server_id=server_id,
            check=check,
            job=self.job(server_id),
            history=self.history(server_id),
            link=(check.link if check else None) or self.get_link(info),
        )

    # ---- updating ------------------------------------------------------------

    def start_update(self, server_id: str, check: UpdateCheck, version_id: str, background: bool = True) -> UpdateJob:
        target = next((v for v in check.newer if v.id == version_id), None)
        if target is None:
            raise UpdateError("That version isn't newer than the one the server runs. Check for updates again.")
        info = self.mgr.get_server(server_id)
        if not info.managed:
            raise ServerConflict("This server wasn't made by the app, so it can't be updated here.")
        if info.status in ("pulling", "creating", "installing", "testing", "missing") or self.mgr.is_building(
            server_id
        ):
            raise ServerConflict("The server is still being set up, or its container is missing.")
        if not os.path.isdir(self.mgr.local_data_path(server_id)):
            raise ServerConflict("The server has no files yet.")
        if info.source == "curseforge" and not settings_store.curseforge_api_key():
            raise UpdateError("Add a CurseForge API key in Settings first.")
        job = UpdateJob(
            phase="stopping",
            message="Starting the update",
            from_version=check.current_name or info.version_id or "the installed version",
            to_version=target.name,
            to_version_id=target.id,
            started_at=_now(),
        )
        with self._lock:
            if server_id in self._jobs:
                raise ServerConflict("This server is already being updated.")
            self._jobs[server_id] = job
        self.mgr.set_busy(server_id, f"Updating to {target.name}")
        args = (info, target, check.link, job)
        if background:
            threading.Thread(target=self._run, args=args, daemon=True, name=f"update-{server_id}").start()
        else:
            self._run(*args)
        return job

    def _set(self, sid: str, job: UpdateJob, phase: str, message: str) -> None:
        with self._lock:
            job.phase, job.message = phase, message
        self.mgr.set_busy(sid, message)

    def _run(self, info: ServerInfo, target: UpdateVersion, link: PackLink | None, job: UpdateJob) -> None:
        sid = info.id
        try:
            self._update(info, target, link, job)
        except Exception as e:  # noqa: BLE001 - the job reports every failure
            log.exception("Update of %s failed", sid)
            self._finish(sid, job, "failed", f"The update stopped with an error: {e}")

    def _update(self, info: ServerInfo, target: UpdateVersion, link: PackLink | None, job: UpdateJob) -> None:
        sid = info.id
        record = self.mgr._load_record(sid) or {}
        old_env = dict(record.get("env") or {})
        if info.source == "curseforge":
            old_env["CF_API_KEY"] = settings_store.curseforge_api_key()
        c = self.mgr._get_container(sid)
        was_running = info.status in ("running", "starting")
        old_build = self._read(self.mgr.build_report_path(sid))

        # 1-2. Stop and back up. Nothing has changed yet if this fails.
        try:
            if c is not None and was_running:
                self._set(sid, job, "stopping", "Stopping the server so the world is saved")
                c.stop(timeout=60)
            self._set(sid, job, "backing-up", "Backing up the whole server folder")
            backup = self.backups.take_backup(sid, "pre-update", "full")
            job.backup_id = backup.id
        except Exception as e:  # noqa: BLE001
            if c is not None and was_running:
                try:
                    c.start()
                except DockerException:
                    pass
            self._finish(sid, job, "failed", f"Couldn't back up the server, so nothing was changed: {e}")
            return

        # 3-4. Install and test-start the new version.
        failure = ""
        try:
            self._set(sid, job, "installing", f"Installing {target.name}")
            new_info, env, build = self._prepare(info, old_env, target, link, job)
            if build is not None:
                from .server_builder import ServerBuilder

                self._set(sid, job, "testing", f"Installing and test-starting {target.name}")
                ServerBuilder(self.mgr).run(new_info, env, build)
                result = self.mgr.get_build(sid)
                job.build = result
                if result is not None and result.phase == "passed":
                    if not was_running:
                        nc = self.mgr._get_container(sid)
                        if nc is not None:
                            nc.stop(timeout=60)
                    tail = "" if was_running else " It was stopped again, as it was before the update."
                    self._finish(sid, job, "passed", f"Updated to {target.name}. {result.message}{tail}")
                    return
                failure = result.message if result else "the test start didn't finish"
            else:
                job.build = self.mgr.get_build(sid)
                failure = job.build.message if job.build else "the pack couldn't be downloaded"
        except Exception as e:  # noqa: BLE001
            log.exception("Installing the update for %s failed", sid)
            failure = str(e)
            job.build = self.mgr.get_build(sid)

        # 5. Put everything back.
        self._set(sid, job, "rolling-back", "The new version didn't work. Restoring the backup")
        try:
            self._rollback(info, old_env, backup.id, old_build, was_running)
        except Exception as e:  # noqa: BLE001
            log.exception("Rolling back %s failed", sid)
            self._finish(
                sid,
                job,
                "failed",
                f"{target.name} didn't start ({failure}), and putting the old version back failed: {e}. "
                f"Restore the backup {backup.id} from Backups.",
            )
            return
        self._finish(
            sid,
            job,
            "rolled-back",
            f"{target.name} didn't start: {failure} The server was put back to {job.from_version} "
            "from the backup, so nothing changed.",
        )

    def _prepare(self, info: ServerInfo, old_env: dict[str, str], target: UpdateVersion, link, job: UpdateJob):
        """The new server info and env, and the build record to carry on (None if the install failed)."""
        updates: dict = {"version_id": target.id}
        if target.minecraft_version:
            updates["minecraft_version"] = target.minecraft_version
        if target.loader:
            updates["loader"] = target.loader
        if target.minecraft_version and target.minecraft_version != info.minecraft_version:
            # The builder moves to a newer Java by itself if the new mods need one.
            updates["java_version"] = pick_java_version(target.minecraft_version)
        env = dict(old_env)
        note = f"Updating from {job.from_version} to {target.name}."

        if info.source == "modrinth":
            env["MODRINTH_VERSION"] = target.id
        elif info.source == "curseforge":
            env["CF_FILE_ID"] = target.id
        elif info.source == "cfimport":
            if link is None or not link.project_id:
                raise UpdateError("The pack's CurseForge link is missing.")
            updates["project_id"] = link.project_id
            if link.slug:
                updates["project_slug"] = link.slug
            env = {k: v for k, v in env.items() if k not in LOADER_KEYS}
            # The old version's mods are in the backup; the new ones are downloaded fresh.
            mods = self.mgr.mods_dir(info.id)
            if os.path.isdir(mods):
                shutil.rmtree(mods)
            new_info = info.model_copy(update=updates)
            env, build = self.mgr._install_cf_import(new_info, env)
            if build is not None:
                build.notes.insert(0, note)
            return new_info, env, build
        build = BuildInfo(phase="installing", started_at=_now(), notes=[note])
        return info.model_copy(update=updates), env, build

    def _rollback(self, info: ServerInfo, old_env: dict[str, str], backup_id: str, old_build, was_running) -> None:
        sid = info.id
        c = self.mgr._get_container(sid)
        if c is not None:
            c.remove(force=True)
        self.backups.restore_now(sid, backup_id)
        path = self.mgr.build_report_path(sid)
        if old_build is None:
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            with open(path, "wb") as f:
                f.write(old_build)
        self.mgr.create_container(info, old_env, start=was_running)

    @staticmethod
    def _read(path: str) -> bytes | None:
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _finish(self, sid: str, job: UpdateJob, phase: str, message: str) -> None:
        with self._lock:
            job.phase, job.message, job.finished_at = phase, message, _now()
            self._jobs.pop(sid, None)
            self._checks.pop(sid, None)
        # A restore puts the old updates.json back, so this job is written after it.
        state = self._load_state(sid)
        history = [job.model_dump(), *(state.get("history") or [])][:HISTORY_KEEP]
        state["history"] = history
        self._save_state(sid, state)
        self.mgr.set_busy(sid, None)
        log.info("Update of %s: %s", sid, message)


def _match_upload(versions: list[UpdateVersion], manifest: dict) -> str:
    """Which CurseForge file an uploaded pack zip is, from its manifest's version."""
    version = _norm(str(manifest.get("version") or ""))
    if not version:
        return ""
    return next((v.id for v in versions if _norm(v.name).endswith(version)), "")
