"""Pack modes: packs that ship alternative configs under config-overrides/<mode>/.

This is the layout used by pack-mode switcher scripts (Nomi-CEu's normal/expert and
others). Switching copies config-overrides/<mode>/ over config/ and records the mode
in .mode, which is all those scripts do, so it works for any pack with the layout and
nothing is downloaded or run. The server is stopped and fully backed up first, and
started again afterwards if it was running.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass, field

from .docker_manager import DockerManager, ServerConflict

log = logging.getLogger(__name__)

OVERRIDES_DIR = "config-overrides"
MODE_FILE = ".mode"
_MODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,40}$")


class PackModeError(Exception):
    pass


@dataclass
class PackModeJob:
    state: str = "idle"  # idle | running | done | failed
    mode: str = ""
    message: str = ""
    backup_id: str = ""
    log: list[str] = field(default_factory=list)


_jobs: dict[str, PackModeJob] = {}
_lock = threading.Lock()


def get_job(server_id: str) -> PackModeJob:
    with _lock:
        return _jobs.get(server_id) or PackModeJob()


class PackModes:
    def __init__(self, mgr: DockerManager, backups):
        self.mgr = mgr
        self.backups = backups

    def _data(self, server_id: str) -> str:
        self.mgr.get_server(server_id)  # raises ServerNotFound
        data = self.mgr.local_data_path(server_id)
        return data if data and os.path.isdir(data) else ""

    def modes(self, data: str) -> list[str]:
        root = os.path.join(data, OVERRIDES_DIR)
        if not os.path.isdir(root):
            return []
        return sorted(
            d for d in os.listdir(root) if _MODE_RE.match(d) and os.path.isdir(os.path.join(root, d))
        )

    @staticmethod
    def current(data: str, modes: list[str]) -> str:
        try:
            with open(os.path.join(data, MODE_FILE)) as f:
                mode = f.readline().strip().lower()
        except OSError:
            mode = ""
        by_lower = {m.lower(): m for m in modes}
        if mode in by_lower:
            return by_lower[mode]
        # The scripts treat a missing .mode as the first/"normal" mode.
        return by_lower.get("normal", "") if not mode else mode

    def info(self, server_id: str) -> dict:
        data = self._data(server_id)
        modes = self.modes(data) if data else []
        job = get_job(server_id)
        return {
            "supported": len(modes) >= 2,
            "modes": modes,
            "current": self.current(data, modes) if modes else "",
            "job": job.__dict__,
        }

    def start_switch(self, server_id: str, mode: str, background: bool = True) -> PackModeJob:
        data = self._data(server_id)
        if not data:
            raise PackModeError("The app can't see this server's files, so it can't switch the pack mode.")
        modes = self.modes(data)
        if mode not in modes:
            raise PackModeError(f"This pack has no '{mode}' mode. Modes: {', '.join(modes) or 'none'}.")
        if self.mgr.busy(server_id):
            raise ServerConflict("The server is busy. Wait for the current job to finish.")
        with _lock:
            if (_jobs.get(server_id) or PackModeJob()).state == "running":
                raise ServerConflict("A pack mode switch is already running.")
            job = _jobs[server_id] = PackModeJob(state="running", mode=mode, message="Starting...")

        def run():
            try:
                self._switch(server_id, data, mode, job)
                job.state = "done"
            except Exception as e:  # noqa: BLE001
                log.exception("Pack mode switch failed for %s", server_id)
                job.state = "failed"
                job.message = f"Switch failed: {e}"
                job.log.append(job.message)
            finally:
                self.mgr.set_busy(server_id, None)

        if background:
            threading.Thread(target=run, daemon=True, name=f"pack-mode-{server_id}").start()
        else:
            run()
        return job

    def _step(self, job: PackModeJob, message: str) -> None:
        job.message = message
        job.log.append(message)

    def _switch(self, server_id: str, data: str, mode: str, job: PackModeJob) -> None:
        info = self.mgr.get_server(server_id)
        was_running = info.status in ("running", "starting")
        if was_running:
            self._step(job, "Stopping the server (it saves the world first)...")
            self.mgr.stop_server(server_id)
        self.mgr.set_busy(server_id, f"Switching pack mode to {mode}")
        self._step(job, "Taking a full backup...")
        backup = self.backups.take_backup(server_id, "pre-script", "full")
        job.backup_id = backup.id
        self._step(job, f"Backup {backup.id} saved. Copying the {mode} configs into config/...")
        copied = self.apply(server_id, data, mode)
        self._step(job, f"Copied {copied} files. Pack mode is now {mode}.")
        self.mgr.set_busy(server_id, None)
        if was_running:
            self._step(job, "Starting the server again...")
            self.mgr.start_server(server_id)
        tail = "" if was_running else " Start it when you're ready."
        self._step(job, f"Done. The server is in {mode} mode.{tail}")

    def apply(self, server_id: str, data: str, mode: str) -> int:
        """cp -rf config-overrides/<mode>/. config/ ; echo <mode> > .mode, keeping file ownership."""
        src = os.path.realpath(os.path.join(data, OVERRIDES_DIR, mode))
        dst = os.path.join(data, "config")
        if os.path.dirname(src) != os.path.realpath(os.path.join(data, OVERRIDES_DIR)):
            raise PackModeError("Unexpected mode folder")
        uid, gid = self.mgr.file_owner(server_id)
        copied = 0
        for here, _dirs, files in os.walk(src):
            rel = os.path.relpath(here, src)
            target_dir = os.path.normpath(os.path.join(dst, rel))
            os.makedirs(target_dir, exist_ok=True)
            _chown(target_dir, uid, gid)
            for name in files:
                target = os.path.join(target_dir, name)
                shutil.copyfile(os.path.join(here, name), target)
                _chown(target, uid, gid)
                copied += 1
        mode_file = os.path.join(data, MODE_FILE)
        with open(mode_file, "w") as f:
            f.write(mode.lower() + "\n")
        _chown(mode_file, uid, gid)
        return copied


def _chown(path: str, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass  # not root (dev/tests): ownership stays as is
