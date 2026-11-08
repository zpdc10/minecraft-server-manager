"""World backups: manual and scheduled zip backups of a server's files, with restore.

Backups are plain zip files in <backup root>/<server id>/, so they can be copied,
opened, or restored by hand without this app. Each zip holds a small manifest
(mcsm-backup.json) next to the backed-up folders.

While a running server is backed up, saving is paused with RCON (save-off, then
save-all flush) so the world files on disk don't change mid-copy, and resumed
with save-on afterwards. itzg/minecraft-server ships rcon-cli and enables RCON by
default, so this goes through `docker exec rcon-cli`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta

from docker.errors import DockerException
from pydantic import BaseModel, Field

from .. import settings_store
from ..config import Settings
from .docker_manager import DockerManager, ServerConflict, ServerNotFound

log = logging.getLogger(__name__)

MANIFEST = "mcsm-backup.json"
KINDS = ("manual", "scheduled", "pre-restore", "pre-update")
# Backups the retention rules may delete. Manual backups are only deleted by the user.
AUTO_KINDS = {"scheduled", "pre-restore", "pre-update"}
BACKUP_NAME_RE = re.compile(r"^(?P<server>[a-z0-9-]+)_(?P<ts>\d{8}-\d{6})_(?P<kind>[a-z-]+)\.zip$")
TS_FORMAT = "%Y%m%d-%H%M%S"
# Small files saved with a world backup: they matter for the world (who may join, who is op).
WORLD_EXTRA_FILES = (
    "server.properties",
    "whitelist.json",
    "ops.json",
    "banned-players.json",
    "banned-ips.json",
    "usercache.json",
)
# Never saved in a full backup: regenerated, or too large to be worth keeping.
FULL_EXCLUDE = {"logs", "crash-reports", "debug", ".mcsm-restore-tmp", ".mcsm-restore-old"}
SKIP_FILES = {"session.lock"}
RESTORE_TMP = ".mcsm-restore-tmp"
RESTORE_OLD = ".mcsm-restore-old"
DEFAULT_BACKUP_MOUNT = "/backups"


class BackupError(Exception):
    pass


class BackupNotFound(Exception):
    pass


class BackupSchedule(BaseModel):
    enabled: bool = False
    # Minutes between scheduled backups.
    interval_minutes: int = Field(default=360, ge=15, le=10080)
    # Newest automatic backups to keep (scheduled and pre-restore). Manual ones are never pruned.
    keep: int = Field(default=10, ge=1, le=500)
    # Also delete automatic backups older than this many days. 0 = no age limit.
    keep_days: int = Field(default=0, ge=0, le=3650)
    # world = world folders plus whitelist/ops/properties; full = the whole server folder.
    scope: str = Field(default="world", pattern="^(world|full)$")


class BackupInfo(BaseModel):
    id: str
    server_id: str
    kind: str
    created_at: str
    size_bytes: int
    scope: str = ""
    # False when the server was running but saving could not be paused, so the copy
    # may have caught a world file mid-write.
    consistent: bool = True
    note: str = ""


class BackupJob(BaseModel):
    action: str  # backup | restore
    kind: str = ""
    backup_id: str = ""
    started_at: str
    message: str = ""


class BackupResult(BaseModel):
    action: str
    ok: bool
    message: str
    finished_at: str


class ServerBackups(BaseModel):
    server_id: str
    backup_dir: str
    schedule: BackupSchedule
    backups: list[BackupInfo]
    job: BackupJob | None = None
    last_result: BackupResult | None = None
    next_scheduled_at: str | None = None


class BackupLocation(BaseModel):
    # Effective backup folder inside this app's container.
    path: str
    # "settings" = chosen on the Settings page, "env" = BACKUP_DIR, "default".
    source: str
    # The same folder on the Docker host (e.g. an Unraid share), when it can be worked out.
    host_path: str = ""
    free_bytes: int | None = None
    total_bytes: int | None = None
    writable: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _result(action: str, ok: bool, message: str) -> BackupResult:
    return BackupResult(action=action, ok=ok, message=message, finished_at=_iso(_now()))


def _safe_member(name: str) -> str | None:
    """A zip member path made safe to extract, or None if it would escape the folder."""
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


class BackupManager:
    def __init__(self, mgr: DockerManager, settings: Settings):
        self.mgr = mgr
        self.settings = settings
        self._lock = threading.Lock()
        self._server_locks: dict[str, threading.Lock] = {}
        self._jobs: dict[str, BackupJob] = {}
        self._results: dict[str, BackupResult] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- location ----------------------------------------------------------

    def location(self) -> BackupLocation:
        saved = (settings_store.load().get("backup_dir") or "").strip()
        if saved:
            path, source = saved, "settings"
        elif self.settings.backup_dir:
            path, source = self.settings.backup_dir, "env"
        elif os.path.isdir(DEFAULT_BACKUP_MOUNT):
            path, source = DEFAULT_BACKUP_MOUNT, "default"
        else:
            path, source = os.path.join(self.settings.data_dir, "backups"), "default"
        loc = BackupLocation(path=path, source=source, host_path=self._host_path(path))
        try:
            os.makedirs(path, exist_ok=True)
            usage = shutil.disk_usage(path)
            loc.free_bytes, loc.total_bytes = usage.free, usage.total
            loc.writable = os.access(path, os.W_OK)
        except OSError:
            pass
        return loc

    @property
    def root(self) -> str:
        return self.location().path

    def set_location(self, path: str) -> BackupLocation:
        path = path.strip()
        if not path:
            settings_store.save({"backup_dir": None})
            return self.location()
        if not os.path.isabs(path):
            raise BackupError("Use an absolute path inside the app container, such as /backups.")
        path = os.path.normpath(path)
        servers = os.path.realpath(self.settings.data_root)
        real = os.path.realpath(path)
        if real == servers or real.startswith(servers + "/"):
            raise BackupError("Backups can't go inside the server data folder.")
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".mcsm-write-test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as e:
            raise BackupError(f"Can't write to {path}: {e.strerror or e}") from e
        settings_store.save({"backup_dir": path})
        return self.location()

    def _host_path(self, path: str) -> str:
        """The host side of a container path, read from this app container's mounts."""
        path = os.path.normpath(path)
        try:
            me = self.mgr.client.containers.get(socket.gethostname())
            mounts = me.attrs.get("Mounts", [])
        except (DockerException, AttributeError, KeyError):
            return ""
        best = None
        for m in mounts:
            dest = os.path.normpath(m.get("Destination", "") or "/nonexistent")
            if path == dest or path.startswith(dest + "/"):
                if best is None or len(dest) > len(best[0]):
                    best = (dest, m.get("Source", ""))
        if best and best[1]:
            return best[1] + path[len(best[0]) :]
        return ""

    def _server_dir(self, server_id: str) -> str:
        if not re.fullmatch(r"[a-z0-9-]+", server_id):
            raise ServerNotFound(server_id)
        return os.path.join(self.root, server_id)

    # ---- schedules -----------------------------------------------------------

    def _schedules_path(self) -> str:
        return os.path.join(self.settings.data_dir, "backup-schedules.json")

    def _load_schedules(self) -> dict[str, dict]:
        try:
            with open(self._schedules_path()) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get_schedule(self, server_id: str) -> BackupSchedule:
        raw = self._load_schedules().get(server_id)
        try:
            return BackupSchedule.model_validate(raw) if raw else BackupSchedule()
        except ValueError:
            return BackupSchedule()

    def set_schedule(self, server_id: str, schedule: BackupSchedule) -> BackupSchedule:
        self.mgr.get_server(server_id)  # raises if unknown
        with self._lock:
            data = self._load_schedules()
            data[server_id] = schedule.model_dump()
            path = self._schedules_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        self.prune(server_id)
        return schedule

    def next_scheduled_at(self, server_id: str, schedule: BackupSchedule | None = None) -> datetime | None:
        schedule = schedule or self.get_schedule(server_id)
        if not schedule.enabled:
            return None
        last = next((b for b in self.list_backups(server_id) if b.kind == "scheduled"), None)
        if last is None:
            return _now()
        return datetime.fromisoformat(last.created_at) + timedelta(minutes=schedule.interval_minutes)

    # ---- listing -------------------------------------------------------------

    def list_backups(self, server_id: str) -> list[BackupInfo]:
        folder = self._server_dir(server_id)
        try:
            names = os.listdir(folder)
        except OSError:
            return []
        out = []
        for name in names:
            m = BACKUP_NAME_RE.match(name)
            if not m or m["server"] != server_id:
                continue
            path = os.path.join(folder, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            created = datetime.strptime(m["ts"], TS_FORMAT).replace(tzinfo=UTC)
            info = BackupInfo(id=name, server_id=server_id, kind=m["kind"], created_at=_iso(created), size_bytes=size)
            manifest = self._read_manifest(path)
            if manifest:
                info.scope = manifest.get("scope", "")
                info.consistent = bool(manifest.get("consistent", True))
                info.note = manifest.get("note", "")
            out.append(info)
        return sorted(out, key=lambda b: b.id.split("_")[1], reverse=True)

    @staticmethod
    def _read_manifest(path: str) -> dict | None:
        try:
            with zipfile.ZipFile(path) as z:
                return json.loads(z.read(MANIFEST))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return None

    def overview(self, server_id: str) -> ServerBackups:
        self.mgr.get_server(server_id)
        schedule = self.get_schedule(server_id)
        nxt = self.next_scheduled_at(server_id, schedule)
        with self._lock:
            job = self._jobs.get(server_id)
            result = self._results.get(server_id)
        loc = self.location()
        return ServerBackups(
            server_id=server_id,
            # Show the host path (e.g. /mnt/user/...) when we know it; that's what the user sees on Unraid.
            backup_dir=os.path.join(loc.host_path or loc.path, server_id),
            schedule=schedule,
            backups=self.list_backups(server_id),
            job=job,
            last_result=result,
            next_scheduled_at=_iso(nxt) if nxt else None,
        )

    def backup_path(self, server_id: str, backup_id: str) -> str:
        m = BACKUP_NAME_RE.match(backup_id)
        if not m or m["server"] != server_id:
            raise BackupNotFound(backup_id)
        path = os.path.join(self._server_dir(server_id), backup_id)
        if not os.path.isfile(path):
            raise BackupNotFound(backup_id)
        return path

    def delete_backup(self, server_id: str, backup_id: str) -> None:
        path = self.backup_path(server_id, backup_id)
        with self._lock:
            job = self._jobs.get(server_id)
        if job and job.backup_id == backup_id:
            raise ServerConflict("That backup is being restored right now.")
        os.remove(path)

    # ---- jobs ------------------------------------------------------------------

    def _server_lock(self, server_id: str) -> threading.Lock:
        with self._lock:
            return self._server_locks.setdefault(server_id, threading.Lock())

    def _check_server(self, server_id: str):
        info = self.mgr.get_server(server_id)  # raises ServerNotFound
        if self.mgr.is_building(server_id) or info.status in ("pulling", "creating", "installing", "testing"):
            raise ServerConflict("The server is still being set up. Back it up once it has finished.")
        if not info.files_available or not self.mgr.local_data_path(server_id):
            raise ServerConflict(info.note or "The app can't see this server's files.")
        if not os.path.isdir(self.mgr.local_data_path(server_id)):
            raise ServerConflict("The server has no files yet.")
        return info

    def _begin(self, server_id: str, job: BackupJob) -> threading.Lock:
        lock = self._server_lock(server_id)
        if not lock.acquire(blocking=False):
            with self._lock:
                running = self._jobs.get(server_id)
            what = f"a {running.action}" if running else "another backup task"
            raise ServerConflict(f"This server is already running {what}. Wait for it to finish.")
        with self._lock:
            self._jobs[server_id] = job
        return lock

    def _end(self, server_id: str, lock: threading.Lock, result: BackupResult) -> None:
        with self._lock:
            self._jobs.pop(server_id, None)
            self._results[server_id] = result
        lock.release()

    def _set_message(self, server_id: str, message: str) -> None:
        with self._lock:
            if server_id in self._jobs:
                self._jobs[server_id] = self._jobs[server_id].model_copy(update={"message": message})

    def start_backup(self, server_id: str, kind: str = "manual", scope: str | None = None, background: bool = True):
        """Validate, then take a backup (in a background thread unless background=False)."""
        if kind not in KINDS:
            raise BackupError(f"Unknown backup kind {kind}")
        self._check_server(server_id)
        scope = scope or self.get_schedule(server_id).scope
        if scope not in ("world", "full"):
            raise BackupError("scope must be 'world' or 'full'")
        job = BackupJob(action="backup", kind=kind, started_at=_iso(_now()), message="Starting backup...")
        lock = self._begin(server_id, job)

        def run():
            try:
                info = self._backup(server_id, kind, scope)
                self.prune(server_id)
                msg = f"Backup {info.id} saved ({_human(info.size_bytes)})."
                if not info.consistent:
                    msg += " Saving couldn't be paused, so the copy may be slightly inconsistent."
                self._end(server_id, lock, _result("backup", True, msg))
            except Exception as e:  # noqa: BLE001 - report every failure to the UI
                log.exception("Backup of %s failed", server_id)
                self._end(server_id, lock, _result("backup", False, f"Backup failed: {e}"))

        if background:
            threading.Thread(target=run, daemon=True, name=f"backup-{server_id}").start()
        else:
            run()
        return job

    def start_restore(self, server_id: str, backup_id: str, background: bool = True) -> BackupJob:
        self._check_server(server_id)
        path = self.backup_path(server_id, backup_id)
        try:
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
        except (OSError, zipfile.BadZipFile) as e:
            raise BackupError(f"The backup file is damaged: {e}") from e
        if bad:
            raise BackupError(f"The backup file is damaged (bad entry {bad}).")
        job = BackupJob(action="restore", backup_id=backup_id, started_at=_iso(_now()), message="Starting restore...")
        lock = self._begin(server_id, job)

        def run():
            try:
                msg = self._restore(server_id, path)
                self.prune(server_id)
                self._end(server_id, lock, _result("restore", True, msg))
            except Exception as e:  # noqa: BLE001
                log.exception("Restore of %s from %s failed", server_id, backup_id)
                self._end(server_id, lock, _result("restore", False, f"Restore failed: {e}"))

        if background:
            threading.Thread(target=run, daemon=True, name=f"restore-{server_id}").start()
        else:
            run()
        return job

    def take_backup(self, server_id: str, kind: str, scope: str) -> BackupInfo:
        """Take a backup now, in this thread, and return it. Raises on failure."""
        job = BackupJob(action="backup", kind=kind, started_at=_iso(_now()), message="Starting backup...")
        lock = self._begin(server_id, job)
        try:
            info = self._backup(server_id, kind, scope)
        except Exception as e:
            self._end(server_id, lock, _result("backup", False, f"Backup failed: {e}"))
            raise
        self._end(server_id, lock, _result("backup", True, f"Backup {info.id} saved ({_human(info.size_bytes)})."))
        return info

    def restore_now(self, server_id: str, backup_id: str) -> str:
        """Put a backup's files back, in this thread, without a safety copy first."""
        path = self.backup_path(server_id, backup_id)
        job = BackupJob(action="restore", backup_id=backup_id, started_at=_iso(_now()), message="Restoring...")
        lock = self._begin(server_id, job)
        try:
            msg = self._restore(server_id, path, safety=False)
        except Exception as e:
            self._end(server_id, lock, _result("restore", False, f"Restore failed: {e}"))
            raise
        self._end(server_id, lock, _result("restore", True, msg))
        return msg

    # ---- RCON --------------------------------------------------------------------

    def _running_container(self, server_id: str):
        c = self.mgr._get_container(server_id)
        if c is None:
            return None
        try:
            c.reload()
        except DockerException:
            pass
        return c if c.attrs.get("State", {}).get("Status", c.status) == "running" else None

    @staticmethod
    def _rcon(container, *command: str) -> tuple[bool, str]:
        try:
            res = container.exec_run(["rcon-cli", *command])
        except DockerException as e:
            return False, str(e)
        out = res.output.decode("utf-8", errors="replace") if isinstance(res.output, bytes) else str(res.output)
        return res.exit_code == 0, out.strip()

    # ---- backup ------------------------------------------------------------------

    def _level_name(self, data: str) -> str:
        try:
            with open(os.path.join(data, "server.properties"), encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("level-name="):
                        name = line.split("=", 1)[1].strip()
                        if name and _safe_member(name) == name and "/" not in name:
                            return name
        except OSError:
            pass
        return "world"

    def world_paths(self, data: str) -> list[str]:
        """Top-level entries of the server folder that make up the world."""
        level = self._level_name(data)
        names = []
        for d in (level, f"{level}_nether", f"{level}_the_end"):
            if os.path.isdir(os.path.join(data, d)):
                names.append(d)
        # Any other top-level folder holding a world (some packs add extra dimensions or worlds).
        try:
            for entry in sorted(os.listdir(data)):
                full = os.path.join(data, entry)
                if entry not in names and os.path.isfile(os.path.join(full, "level.dat")):
                    names.append(entry)
        except OSError:
            pass
        names += [f for f in WORLD_EXTRA_FILES if os.path.isfile(os.path.join(data, f))]
        return names

    def _full_paths(self, data: str) -> list[str]:
        return sorted(e for e in os.listdir(data) if e not in FULL_EXCLUDE)

    def _backup(self, server_id: str, kind: str, scope: str) -> BackupInfo:
        info = self.mgr.get_server(server_id)
        data = self.mgr.local_data_path(server_id)
        paths = self.world_paths(data) if scope == "world" else self._full_paths(data)
        if scope == "world" and not any(os.path.isdir(os.path.join(data, p)) for p in paths):
            raise BackupError("No world folder found yet. Start the server once so it creates the world.")

        folder = self._server_dir(server_id)
        os.makedirs(folder, exist_ok=True)
        ts = _now()
        name = f"{server_id}_{ts.strftime(TS_FORMAT)}_{kind}.zip"
        while os.path.exists(os.path.join(folder, name)):
            ts += timedelta(seconds=1)
            name = f"{server_id}_{ts.strftime(TS_FORMAT)}_{kind}.zip"
        final = os.path.join(folder, name)
        partial = final + ".partial"

        container = self._running_container(server_id)
        paused, note = False, ""
        if container is not None:
            self._set_message(server_id, "Pausing world saves...")
            ok, out = self._rcon(container, "save-off")
            if ok:
                paused = True
                ok, out = self._rcon(container, "save-all", "flush")
                if not ok:
                    note = f"save-all failed: {out[:200]}"
                time.sleep(self.settings.backup_flush_wait)
            else:
                note = f"Could not pause saving over RCON: {out[:200]}"
                log.warning("Backup of %s: %s", server_id, note)
        try:
            self._set_message(server_id, "Copying world files...")
            manifest = {
                "server_id": server_id,
                "server_name": info.name,
                "created_at": _iso(ts),
                "kind": kind,
                "scope": scope,
                "paths": paths,
                "minecraft_version": info.minecraft_version,
                "pack_title": info.pack_title,
                "consistent": container is None or paused,
                "note": note,
            }
            try:
                with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                    for p in paths:
                        self._add(z, data, p)
                    z.writestr(MANIFEST, json.dumps(manifest, indent=1))
            except BaseException:
                try:
                    os.remove(partial)
                except OSError:
                    pass
                raise
        finally:
            if paused:
                ok, out = self._rcon(container, "save-on")
                if not ok:
                    log.error("Backup of %s: save-on failed, world saving may still be off: %s", server_id, out)
        os.replace(partial, final)
        log.info("Backed up %s to %s", server_id, final)
        return BackupInfo(
            id=name,
            server_id=server_id,
            kind=kind,
            created_at=_iso(ts),
            size_bytes=os.path.getsize(final),
            scope=scope,
            consistent=manifest["consistent"],
            note=note,
        )

    @staticmethod
    def _add(z: zipfile.ZipFile, base: str, rel: str) -> None:
        full = os.path.join(base, rel)
        if os.path.islink(full):
            return
        if os.path.isfile(full):
            z.write(full, rel)
            return
        for root, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            rel_root = os.path.relpath(root, base)
            if not files and not dirs:
                z.writestr(rel_root.replace(os.sep, "/") + "/", "")
            for f in files:
                if f in SKIP_FILES:
                    continue
                path = os.path.join(root, f)
                if os.path.islink(path):
                    continue
                try:
                    z.write(path, os.path.join(rel_root, f))
                except FileNotFoundError:
                    pass  # deleted while we walked (e.g. a temp file)

    # ---- restore -----------------------------------------------------------------

    def _restore(self, server_id: str, path: str, safety: bool = True) -> str:
        data = self.mgr.local_data_path(server_id)
        manifest = self._read_manifest(path) or {}
        scope = manifest.get("scope") if manifest.get("scope") in ("world", "full") else "world"

        tmp = os.path.join(data, RESTORE_TMP)
        old = os.path.join(data, RESTORE_OLD)
        for d in (tmp, old):
            shutil.rmtree(d, ignore_errors=True)

        # 1. Unpack into a staging folder first, so a bad zip leaves the server untouched.
        self._set_message(server_id, "Unpacking backup...")
        top: set[str] = set()
        with zipfile.ZipFile(path) as z:
            for member in z.infolist():
                if member.filename == MANIFEST:
                    continue
                safe = _safe_member(member.filename)
                if safe is None:
                    raise BackupError(f"Backup contains an unsafe path: {member.filename}")
                top.add(safe.split("/", 1)[0])
                target = os.path.join(tmp, safe)
                if member.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
        if not top:
            shutil.rmtree(tmp, ignore_errors=True)
            raise BackupError("The backup is empty.")

        # 2. Stop the server so it doesn't overwrite the restored world on shutdown.
        c = self.mgr._get_container(server_id)
        was_running = self._running_container(server_id) is not None
        if was_running:
            self._set_message(server_id, "Stopping the server...")
            c.stop(timeout=60)

        try:
            # 3. Safety copy of what's there now, in case the restore was a mistake.
            self._set_message(server_id, "Saving a safety backup of the current world...")
            current = self.world_paths(data) if scope == "world" else self._full_paths(data)
            current = [p for p in current if p not in (RESTORE_TMP, RESTORE_OLD)]
            want_safety, safety = safety, ""
            if want_safety and any(os.path.exists(os.path.join(data, p)) for p in current):
                try:
                    safety = self._backup(server_id, "pre-restore", scope).id
                except BackupError:
                    safety = ""

            # 4. Swap the restored entries in.
            self._set_message(server_id, "Replacing world files...")
            os.makedirs(old, exist_ok=True)
            for name in top:
                live = os.path.join(data, name)
                if os.path.lexists(live):
                    os.replace(live, os.path.join(old, name))
            for name in top:
                os.replace(os.path.join(tmp, name), os.path.join(data, name))
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(tmp, ignore_errors=True)
            for name in top:
                self._chown_tree(os.path.join(data, name), *self.mgr.file_owner(server_id))
        finally:
            if was_running and c is not None:
                self._set_message(server_id, "Starting the server...")
                try:
                    c.start()
                except DockerException as e:
                    log.error("Could not restart %s after restore: %s", server_id, e)

        msg = f"Restored {os.path.basename(path)}."
        if safety:
            msg += f" The previous world was saved as {safety}."
        if was_running:
            msg += " The server is starting again."
        return msg

    def _chown_tree(self, path: str, uid: int | None = None, gid: int | None = None) -> None:
        if uid is None or gid is None:
            uid, gid = self.settings.mc_uid, self.settings.mc_gid
        try:
            os.lchown(path, uid, gid)
            for root, dirs, files in os.walk(path):
                for n in dirs + files:
                    os.lchown(os.path.join(root, n), uid, gid)
        except OSError:
            pass  # not running as root (tests, dev); itzg fixes ownership on start

    # ---- retention -------------------------------------------------------------

    def prune(self, server_id: str) -> list[str]:
        """Delete automatic backups beyond the schedule's keep count and age limit."""
        schedule = self.get_schedule(server_id)
        auto = [b for b in self.list_backups(server_id) if b.kind in AUTO_KINDS]
        doomed = auto[schedule.keep :]
        if schedule.keep_days and auto:
            cutoff = _now() - timedelta(days=schedule.keep_days)
            # Always keep the newest automatic backup, however old.
            doomed += [b for b in auto[1 : schedule.keep] if datetime.fromisoformat(b.created_at) < cutoff]
        removed = []
        for b in doomed:
            try:
                os.remove(os.path.join(self._server_dir(server_id), b.id))
                removed.append(b.id)
            except OSError as e:
                log.warning("Could not delete old backup %s: %s", b.id, e)
        if removed:
            log.info("Pruned %d old backups of %s", len(removed), server_id)
        return removed

    # ---- scheduler ---------------------------------------------------------------

    def run_due(self) -> list[str]:
        """Take every scheduled backup that is due. Returns the server ids backed up."""
        done = []
        for server_id, raw in self._load_schedules().items():
            try:
                schedule = BackupSchedule.model_validate(raw)
            except ValueError:
                continue
            if not schedule.enabled:
                continue
            try:
                nxt = self.next_scheduled_at(server_id, schedule)
                if nxt is None or nxt > _now():
                    continue
                if not self._changed_since_last_backup(server_id):
                    continue
                self.start_backup(server_id, kind="scheduled", scope=schedule.scope, background=False)
                done.append(server_id)
            except (ServerNotFound, ServerConflict, BackupError) as e:
                log.debug("Scheduled backup of %s skipped: %s", server_id, e)
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("Scheduled backup of %s failed", server_id)
        return done

    def _changed_since_last_backup(self, server_id: str) -> bool:
        """False when the server is stopped and nothing in the world changed since the last backup."""
        if self._running_container(server_id) is not None:
            return True
        backups = self.list_backups(server_id)
        if not backups:
            return True
        last = datetime.fromisoformat(backups[0].created_at).timestamp()
        data = self.mgr.local_data_path(server_id)
        for p in self.world_paths(data):
            full = os.path.join(data, p)
            for f in (os.path.join(full, "level.dat"), full):
                try:
                    if os.path.getmtime(f) > last:
                        return True
                except OSError:
                    continue
        return False

    def start_scheduler(self, interval: float = 60.0) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.wait(interval):
                try:
                    self.run_due()
                except Exception:  # noqa: BLE001
                    log.exception("Backup scheduler error")

        self._thread = threading.Thread(target=loop, daemon=True, name="backup-scheduler")
        self._thread.start()

    def stop_scheduler(self) -> None:
        self._stop.set()


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
