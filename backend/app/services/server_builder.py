"""Build a working server from a modpack, even one made only for the client.

Steps:

1. Install: run the itzg container with SETUP_ONLY=true so it downloads the pack,
   the mod loader and the mods, then exits without starting Minecraft.
2. Scan: read every jar in mods/ and set aside the ones that declare themselves
   client-only or are on the shared / learned list of client-only mods.
3. Test: start the server with no auto-restart and watch the log. If it prints
   "Done (...)! For help", the build passed: turn auto-restart on and leave it running.
   If it exits, read the log and new crash reports, work out which mods broke it
   (see crash_analyzer), set those aside and try again.

Removed jars are moved to .mcsm/removed-mods/ in the server folder, so they can be
put back by hand. They are also passed to itzg as exclusions / ignored missing
files so its next install check doesn't download them again.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import time
from datetime import UTC, datetime

from docker.errors import DockerException, NotFound

from ..models.server import BuildAttempt, BuildInfo, RemovedMod, ServerInfo
from .crash_analyzer import DONE_RE, analyze
from .mod_db import get_mod_db
from .mod_scan import ModJar, scan_mods

log = logging.getLogger(__name__)

POLL_SECONDS = 3
JAVA_TAGS = [(8, "java8"), (11, "java11"), (16, "java16"), (17, "java17"), (21, "java21")]
REASON_TEXT = {
    "client_only": "Client-only mod",
    "known": "On the list of client-only mods",
    "missing_dependency": "Needs a mod that isn't on the server",
    "crash": "Crashed the server",
}


class BuildCancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def java_tag_for(major: int) -> str:
    for version, tag in JAVA_TAGS:
        if version >= major:
            return tag
    return "latest"


def _java_major(tag: str) -> int:
    for version, t in JAVA_TAGS:
        if t == tag:
            return version
    return 99


def _join(existing: str, items: list[str]) -> str:
    parts = [p for p in (existing or "").replace(",", "\n").splitlines() if p.strip()]
    return "\n".join(dict.fromkeys(parts + items))


class ServerBuilder:
    def __init__(self, mgr, sleep=time.sleep, clock=time.monotonic):
        self.mgr = mgr
        self.settings = mgr.settings
        self.db = get_mod_db(mgr.settings)
        self.sleep = sleep
        self.clock = clock

    # ---- helpers ---------------------------------------------------------------

    def _check(self, server_id: str) -> None:
        if self.mgr.is_cancelled(server_id):
            raise BuildCancelled()

    def _env(self, info: ServerInfo, env: dict[str, str], removed: list[RemovedMod]) -> dict[str, str]:
        files = [r.file for r in removed]
        if not files:
            return dict(env)
        paths = [f"mods/{f}" for f in files]
        env = dict(env)
        prefix = "MODRINTH" if info.source == "modrinth" else "CF"
        if prefix == "MODRINTH":
            env["MODRINTH_EXCLUDE_FILES"] = _join(env.get("MODRINTH_EXCLUDE_FILES", ""), files)
        env[f"{prefix}_IGNORE_MISSING_FILES"] = _join(env.get(f"{prefix}_IGNORE_MISSING_FILES", ""), paths)
        env[f"{prefix}_OVERRIDES_EXCLUSIONS"] = _join(env.get(f"{prefix}_OVERRIDES_EXCLUSIONS", ""), paths)
        return env

    def _remove(self, info: ServerInfo, build: BuildInfo, jar: ModJar, reason: str, detail: str) -> bool:
        src = os.path.join(self.mgr.mods_dir(info.id), jar.filename)
        dest_dir = os.path.join(self.mgr.local_data_path(info.id), ".mcsm", "removed-mods")
        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(src, os.path.join(dest_dir, jar.filename))
        except OSError as e:
            log.warning("Could not remove %s: %s", src, e)
            return False
        build.removed.append(
            RemovedMod(
                file=jar.filename,
                name=jar.name,
                mod_ids=jar.mod_ids,
                reason=reason,
                detail=detail,
                attempt=build.attempt,
            )
        )
        return True

    def _wait_exit(self, container, server_id: str, timeout: int) -> int | None:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            self._check(server_id)
            try:
                container.reload()
            except NotFound as e:
                raise BuildCancelled() from e
            state = container.attrs.get("State", {})
            if state.get("Status") in ("exited", "dead"):
                return int(state.get("ExitCode", 1))
            self.sleep(POLL_SECONDS)
        return None

    def _watch_start(self, container, server_id: str) -> str:
        """Wait until the server is up ("started"), exits ("crashed") or takes too long ("timeout")."""
        deadline = self.clock() + self.settings.build_start_timeout
        while self.clock() < deadline:
            self._check(server_id)
            try:
                container.reload()
                tail = container.logs(tail=400).decode("utf-8", errors="replace")
            except NotFound as e:
                raise BuildCancelled() from e
            if DONE_RE.search(tail):
                return "started"
            if container.attrs.get("State", {}).get("Status") in ("exited", "dead"):
                return "crashed"
            self.sleep(POLL_SECONDS)
        return "timeout"

    def _crash_text(self, container, server_id: str, since: float) -> str:
        try:
            text = container.logs(tail=6000).decode("utf-8", errors="replace")
        except DockerException:
            text = ""
        reports = glob.glob(os.path.join(self.mgr.local_data_path(server_id), "crash-reports", "*.txt"))
        for path in sorted(reports, key=os.path.getmtime):
            try:
                if os.path.getmtime(path) >= since - 5:
                    with open(path, errors="replace") as f:
                        text += "\n" + f.read()
            except OSError:
                pass
        return text

    # ---- the build ---------------------------------------------------------------

    def run(self, info: ServerInfo, env: dict[str, str]) -> None:
        build = BuildInfo(
            phase="installing",
            message="Downloading the modpack and its mods",
            max_attempts=self.settings.build_max_attempts,
            started_at=_now(),
        )
        self.mgr.save_build(info.id, build)
        try:
            self._run(info, env, build)
        except BuildCancelled:
            log.info("Build of %s cancelled", info.id)
            with self.mgr._lock:
                self.mgr._builds.pop(info.id, None)
                self.mgr._cancelled.discard(info.id)
            c = self.mgr._get_container(info.id)
            if c is not None:
                c.remove(force=True)
        except (DockerException, OSError) as e:
            log.exception("Build of %s failed", info.id)
            msg = getattr(e, "explanation", None) or str(e)
            self._fail(info, build, f"Build stopped by an error: {msg}")

    def _fail(self, info: ServerInfo, build: BuildInfo, message: str) -> None:
        build.phase, build.message, build.finished_at = "failed", message, _now()
        self.mgr.finish_build(info.id, build)

    def _run(self, info: ServerInfo, env: dict[str, str], build: BuildInfo) -> None:
        sid = info.id
        # 1. Install only.
        self._check(sid)
        c = self.mgr.create_container(info, {**env, "SETUP_ONLY": "true"}, restart=False)
        code = self._wait_exit(c, sid, self.settings.build_install_timeout)
        if code != 0:
            logs = c.logs(tail=4000).decode("utf-8", errors="replace")
            a = analyze(logs, [])
            build.attempts.append(BuildAttempt(number=0, result="install_failed", summary=a.headline))
            why = "it took too long" if code is None else (a.headline or f"exit code {code}")
            self._fail(info, build, f"Installing the modpack failed: {why}. Check the logs.")
            return

        # 2. Set aside mods that say they are client-only, or are known to be.
        build.phase, build.message = "scanning", "Checking mods for client-only ones"
        self.mgr.save_build(sid, build)
        match = self.db.matcher(info.loader)
        for jar in scan_mods(self.mgr.mods_dir(sid)):
            if jar.client_only:
                self._remove(info, build, jar, "client_only", "The mod's own metadata says it only runs on the client.")
            elif m := match(jar):
                where = "this app's shared list" if m.source == "shared" else "mods learned from earlier builds"
                note = f" ({m.note})" if m.note else ""
                self._remove(info, build, jar, "known", f"Listed as client-only in {where}{note}.")

        # 3. Test-start and fix until it comes up.
        crash_removals = 0
        for attempt in range(1, self.settings.build_max_attempts + 1):
            self._check(sid)
            build.phase, build.attempt = "testing", attempt
            build.message = f"Test-starting the server (attempt {attempt} of {build.max_attempts})"
            self.mgr.save_build(sid, build)
            started = time.time()
            c = self.mgr.create_container(info, self._env(info, env, build.removed), restart=False)
            outcome = self._watch_start(c, sid)

            if outcome == "started":
                c.update(restart_policy={"Name": "unless-stopped"})
                build.attempts.append(BuildAttempt(number=attempt, result="started"))
                build.phase, build.finished_at = "passed", _now()
                n = len(build.removed)
                build.message = f"Server started on attempt {attempt}" + (
                    f" after removing {n} mod{'s' if n != 1 else ''}." if n else "."
                )
                if n and not scan_mods(self.mgr.mods_dir(sid)):
                    loader = (info.loader or "modded").capitalize()
                    build.message += (
                        f" Every mod in this pack is client-side, so this is a plain {loader} server "
                        "that players with the pack can join."
                    )
                self.mgr.finish_build(sid, build)
                return

            if outcome == "timeout":
                c.update(restart_policy={"Name": "unless-stopped"})
                build.attempts.append(BuildAttempt(number=attempt, result="timeout"))
                minutes = self.settings.build_start_timeout // 60
                self._fail(
                    info,
                    build,
                    f"The server was still starting after {minutes} minutes, so the test stopped watching. "
                    "It was left running; check the logs.",
                )
                return

            text = self._crash_text(c, sid, started)
            jars = scan_mods(self.mgr.mods_dir(sid))
            a = analyze(text, jars)
            record = BuildAttempt(number=attempt, result="crashed", summary=a.headline)
            build.attempts.append(record)

            if a.java_needed and a.java_needed > _java_major(info.java_version):
                new_tag = java_tag_for(a.java_needed)
                record.summary = f"Mods need Java {a.java_needed}; switching from {info.java_version} to {new_tag}"
                self.mgr.pull_image(new_tag)
                info = info.model_copy(update={"java_version": new_tag})
                continue
            if a.fatal:
                self._fail(info, build, a.fatal)
                return
            if not a.suspects:
                why = f": {a.headline}" if a.headline else ""
                self._fail(info, build, f"The server crashed and no single mod could be blamed{why}. Check the logs.")
                return
            if crash_removals + len(a.suspects) > self.settings.build_max_removals:
                self._fail(
                    info,
                    build,
                    f"Stopped after removing {crash_removals} mods for crashes, to avoid gutting the pack. "
                    f"Last error: {a.headline}",
                )
                return
            for s in a.suspects:
                if self._remove(info, build, s.jar, s.reason, s.detail):
                    record.removed.append(s.jar.filename)
                    crash_removals += 1
                    self.db.learn(s.jar, s.reason, s.detail, info.pack_title or info.project_slug, info.loader)
            if not record.removed:
                self._fail(info, build, f"The server crashed and the broken mod couldn't be removed: {a.headline}")
                return

        self._fail(info, build, f"The server still didn't start after {build.max_attempts} attempts. Check the logs.")
