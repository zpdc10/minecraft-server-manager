"""Day-to-day management of a running server: console, players, config files, settings.

Commands go to the server through rcon-cli inside the itzg container (docker exec),
so no RCON port has to be exposed. Config files are read from the server's data folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import UTC, datetime

from docker.errors import DockerException, ImageNotFound

from ..models.admin import (
    ConsoleResult,
    ModFile,
    PlayerEntry,
    PlayerLists,
    PropertiesInfo,
    ServerStats,
    UpdateSettingsRequest,
)
from ..models.server import ServerInfo
from .docker_manager import BUILD_ACTIVE_PHASES, DockerManager, ServerConflict

# Minecraft colour/format codes and ANSI escapes that rcon-cli passes through.
_FORMAT_RE = re.compile(r"\x1b\[[0-9;]*m|§.")
_LIST_RE = re.compile(r"(\d+)\s*(?:of a max of|/)\s*(\d+)[^:]*:?(.*)", re.S)
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
_IP_RE = re.compile(r"^[0-9a-fA-F:.]{3,45}$")

# server.properties keys the app or the itzg image control. Editing them in the file
# would not stick (or would break the container), so the editor shows them read-only.
ALWAYS_LOCKED = {
    "server-port": "The container always listens on 25565. Change the port in Settings.",
    "enable-rcon": "Needed for the console.",
    "rcon.port": "Needed for the console.",
    "rcon.password": "Needed for the console.",
}
# itzg env var -> property it overwrites on every start (when the variable is set).
ENV_PROPERTIES = {
    "MOTD": "motd",
    "SERVER_NAME": "server-name",
    "DIFFICULTY": "difficulty",
    "MODE": "gamemode",
    "MAX_PLAYERS": "max-players",
    "PVP": "pvp",
    "LEVEL": "level-name",
    "SEED": "level-seed",
    "LEVEL_TYPE": "level-type",
    "ONLINE_MODE": "online-mode",
    "ALLOW_FLIGHT": "allow-flight",
    "VIEW_DISTANCE": "view-distance",
    "SIMULATION_DISTANCE": "simulation-distance",
    "SPAWN_PROTECTION": "spawn-protection",
    "HARDCORE": "hardcore",
    "ENABLE_WHITELIST": "white-list",
    "ENFORCE_WHITELIST": "enforce-whitelist",
}
# Env set by this app that the settings editor manages; everything else is kept as-is.
_OWN_ENV = {"UID", "GID"}
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


class AdminError(ValueError):
    """Bad input for an admin action (maps to HTTP 400)."""


def clean_output(text: str) -> str:
    return _FORMAT_RE.sub("", text).strip()


def parse_player_list(text: str) -> tuple[int, int, list[str]]:
    """Parse the reply to `list`: "There are 2 of a max of 20 players online: a, b"."""
    m = _LIST_RE.search(clean_output(text))
    if not m:
        return 0, 0, []
    names = [n.strip() for n in m.group(3).replace("\n", ",").split(",")]
    # Some servers prefix names with a rank, e.g. "default: Steve"; keep the last word.
    names = [n.split()[-1] for n in names if n]
    return int(m.group(1)), int(m.group(2)), names


def parse_properties(text: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#!":
            continue
        key, sep, value = s.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    return props


def render_properties(original: str, values: dict[str, str]) -> str:
    """Update values in a server.properties text, keeping comments and key order."""
    out, seen = [], set()
    for line in original.splitlines():
        s = line.strip()
        key, sep, _ = s.partition("=")
        key = key.strip()
        if s and s[0] not in "#!" and sep and key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    return "\n".join(out) + "\n"


def _env_dict(env_list: list[str] | None) -> dict[str, str]:
    env = {}
    for item in env_list or []:
        k, _, v = item.partition("=")
        env[k] = v
    return env


class ServerAdmin:
    def __init__(self, mgr: DockerManager):
        self.mgr = mgr

    # ---- helpers -----------------------------------------------------------

    def _running(self, server_id: str):
        c = self.mgr._require(server_id)
        c.reload()
        if c.attrs.get("State", {}).get("Status") != "running":
            raise ServerConflict("The server is not running. Start it first.")
        return c

    def _path(self, server_id: str, *parts: str) -> str:
        self.mgr.get_server(server_id)  # 404 for unknown servers
        return os.path.join(self.mgr.local_data_path(server_id), *parts)

    def _chown(self, path: str) -> None:
        try:
            os.chown(path, self.mgr.settings.mc_uid, self.mgr.settings.mc_gid)
        except OSError:
            pass

    def _write(self, path: str, text: str) -> None:
        tmp = path + ".mcsm-tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
        self._chown(path)

    # ---- console -----------------------------------------------------------

    def rcon(self, server_id: str, command: str) -> str:
        command = command.strip().lstrip("/")
        if not command:
            raise AdminError("Enter a command")
        if "\n" in command or "\r" in command:
            raise AdminError("Send one command at a time")
        c = self._running(server_id)
        code, out = c.exec_run(["rcon-cli", command])
        text = clean_output((out or b"").decode("utf-8", errors="replace"))
        if code != 0:
            raise ServerConflict(text or "The server did not accept the command. It may still be starting.")
        return text

    def console(self, server_id: str, command: str) -> ConsoleResult:
        return ConsoleResult(command=command.strip().lstrip("/"), output=self.rcon(server_id, command))

    # ---- players -----------------------------------------------------------

    def _read_json_list(self, server_id: str, name: str) -> list[dict]:
        try:
            with open(self._path(server_id, name)) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def players(self, server_id: str) -> PlayerLists:
        info = self.mgr.get_server(server_id)
        props = self._read_properties(server_id)
        lists = PlayerLists(
            running=info.status in ("running", "starting"),
            whitelist_enabled=props.get("white-list", "false") == "true",
            whitelist=[
                PlayerEntry(name=e.get("name", ""), uuid=e.get("uuid", ""))
                for e in self._read_json_list(server_id, "whitelist.json")
            ],
            ops=[
                PlayerEntry(name=e.get("name", ""), uuid=e.get("uuid", ""), level=e.get("level"))
                for e in self._read_json_list(server_id, "ops.json")
            ],
            banned=[
                PlayerEntry(name=e.get("name", ""), uuid=e.get("uuid", ""), reason=e.get("reason", ""))
                for e in self._read_json_list(server_id, "banned-players.json")
            ],
            banned_ips=[
                PlayerEntry(name=e.get("ip", ""), reason=e.get("reason", ""))
                for e in self._read_json_list(server_id, "banned-ips.json")
            ],
        )
        if lists.running:
            try:
                count, max_players, names = parse_player_list(self.rcon(server_id, "list"))
                lists.online, lists.max_players, lists.online_count = names, max_players, count
                lists.console_ready = True
            except (ServerConflict, DockerException):
                pass  # still booting; RCON comes up last
        return lists

    PLAYER_ACTIONS = {
        "whitelist_add": "whitelist add {name}",
        "whitelist_remove": "whitelist remove {name}",
        "op": "op {name}",
        "deop": "deop {name}",
        "kick": "kick {name} {reason}",
        "ban": "ban {name} {reason}",
        "pardon": "pardon {name}",
        "ban_ip": "ban-ip {name} {reason}",
        "pardon_ip": "pardon-ip {name}",
    }

    def player_action(self, server_id: str, action: str, name: str, reason: str = "") -> ConsoleResult:
        template = self.PLAYER_ACTIONS.get(action)
        if template is None:
            raise AdminError(f"Unknown action {action}")
        name = name.strip()
        valid = _IP_RE if action in ("ban_ip", "pardon_ip") else _NAME_RE
        if not valid.match(name):
            raise AdminError(f"'{name}' is not a valid player name" if valid is _NAME_RE else f"'{name}' is not an IP")
        reason = " ".join(reason.split())[:200]
        cmd = template.format(name=name, reason=reason).strip()
        return ConsoleResult(command=cmd, output=self.rcon(server_id, cmd))

    def set_whitelist(self, server_id: str, enabled: bool) -> ConsoleResult:
        cmd = "whitelist on" if enabled else "whitelist off"
        try:
            return ConsoleResult(command=cmd, output=self.rcon(server_id, cmd))
        except ServerConflict:
            if self.mgr.get_server(server_id).status in ("running", "starting"):
                raise
        # Stopped: edit the file; it takes effect on the next start.
        self.save_properties(server_id, {"white-list": "true" if enabled else "false"})
        return ConsoleResult(command=cmd, output="Saved. It takes effect when the server starts.")

    # ---- server.properties ---------------------------------------------------

    def _read_properties(self, server_id: str) -> dict[str, str]:
        try:
            with open(self._path(server_id, "server.properties")) as f:
                return parse_properties(f.read())
        except OSError:
            return {}

    def _locked(self, server_id: str) -> dict[str, str]:
        locked = dict(ALWAYS_LOCKED)
        c = self.mgr._get_container(server_id)
        env = _env_dict(c.attrs.get("Config", {}).get("Env")) if c is not None else {}
        for var, prop in ENV_PROPERTIES.items():
            if var in env:
                hint = " Change it in Settings." if var == "MOTD" else ""
                locked.setdefault(prop, f"Set by the container's {var} variable on every start.{hint}")
        return locked

    def properties(self, server_id: str) -> PropertiesInfo:
        path = self._path(server_id, "server.properties")
        return PropertiesInfo(
            exists=os.path.isfile(path),
            properties=self._read_properties(server_id),
            locked=self._locked(server_id),
        )

    def save_properties(self, server_id: str, values: dict[str, str]) -> PropertiesInfo:
        path = self._path(server_id, "server.properties")
        if not os.path.isfile(path):
            raise ServerConflict("server.properties doesn't exist yet. Start the server once to create it.")
        locked = self._locked(server_id)
        current = self._read_properties(server_id)
        changes = {}
        for key, value in values.items():
            key, value = str(key).strip(), str(value)
            if not key or any(ch in key for ch in "=:#\n\r ") or "\n" in value or "\r" in value:
                raise AdminError(f"Invalid property {key!r}")
            if current.get(key) == value:
                continue
            if key in locked:
                raise AdminError(f"{key} can't be edited here: {locked[key]}")
            changes[key] = value
        if changes:
            with open(path) as f:
                original = f.read()
            self._write(path, render_properties(original, changes))
        return self.properties(server_id)

    # ---- settings (recreates the container) -----------------------------------

    def update_settings(self, server_id: str, req: UpdateSettingsRequest) -> ServerInfo:
        mgr = self.mgr
        c = mgr._require(server_id)
        info = mgr.get_server(server_id)
        if info.build is not None and info.build.phase in BUILD_ACTIVE_PHASES:
            raise ServerConflict("The server is still being built and tested. Wait for it to finish.")
        changes = req.model_dump(exclude_none=True)
        motd = changes.pop("motd", None)
        if "port" in changes and changes["port"] != info.port:
            used = {p: n for p, n in mgr._used_ports().items() if p != info.port}
            if changes["port"] in used:
                raise ServerConflict(f"Port {changes['port']} is already used by server '{used[changes['port']]}'")
        new = info.model_copy(update=changes)

        old_env = self._user_env(c)
        env = dict(old_env)
        env["MEMORY"] = f"{new.memory_gb}G"
        if motd is not None:
            env["MOTD"] = motd.replace("\n", " ")
        if new == info and env == old_env:
            return info

        if new.java_version != info.java_version:
            mgr.pull_image(new.java_version)
        was_running = c.attrs.get("State", {}).get("Status") in ("running", "restarting")
        if was_running:
            c.stop(timeout=60)
        try:
            container = mgr.create_container(new, env, start=was_running)
        except DockerException:
            # Put the old settings back so the server isn't left without a container.
            mgr.create_container(info, old_env, start=was_running)
            raise
        container.reload()
        return mgr._info_from_container(container)

    def _user_env(self, c) -> dict[str, str]:
        """The container's env minus what the image and create_container add."""
        env = _env_dict(c.attrs.get("Config", {}).get("Env"))
        image_env: dict[str, str] = {}
        try:
            image = c.attrs.get("Config", {}).get("Image") or c.attrs.get("Image")
            if image:
                image_env = _env_dict(self.mgr.client.images.get(image).attrs.get("Config", {}).get("Env"))
        except (ImageNotFound, DockerException, AttributeError):
            pass
        return {k: v for k, v in env.items() if k not in _OWN_ENV and image_env.get(k) != v}

    # ---- stats ---------------------------------------------------------------

    def stats(self, server_id: str) -> ServerStats:
        c = self.mgr._require(server_id)
        c.reload()
        if c.attrs.get("State", {}).get("Status") != "running":
            return ServerStats(running=False)
        s = c.stats(stream=False)
        cpu, pre = s.get("cpu_stats", {}), s.get("precpu_stats", {})
        cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
        cpu_pct = (cpu_delta / sys_delta) * ncpu * 100 if sys_delta > 0 and cpu_delta > 0 else 0.0
        mem = s.get("memory_stats", {})
        stats = mem.get("stats", {})
        cache = stats.get("inactive_file", stats.get("total_inactive_file", stats.get("cache", 0)))
        used = max(0, mem.get("usage", 0) - cache)
        return ServerStats(
            running=True,
            cpu_percent=round(cpu_pct, 1),
            cpu_count=ncpu,
            memory_used_mb=round(used / 2**20),
            memory_limit_mb=round(mem.get("limit", 0) / 2**20),
            started_at=c.attrs.get("State", {}).get("StartedAt", ""),
        )

    # ---- mods ------------------------------------------------------------------

    def _mods_dir(self, server_id: str) -> str:
        return self._path(server_id, "mods")

    def _mod_path(self, server_id: str, filename: str) -> str:
        if not filename or filename != os.path.basename(filename) or filename.startswith("."):
            raise AdminError("Invalid file name")
        return os.path.join(self._mods_dir(server_id), filename)

    def list_mods(self, server_id: str) -> list[ModFile]:
        d = self._mods_dir(server_id)
        if not os.path.isdir(d):
            return []
        mods = []
        for name in sorted(os.listdir(d), key=str.lower):
            p = os.path.join(d, name)
            if not os.path.isfile(p) or not (name.endswith(".jar") or name.endswith(".jar.disabled")):
                continue
            st = os.stat(p)
            mods.append(
                ModFile(
                    file=name,
                    enabled=name.endswith(".jar"),
                    size=st.st_size,
                    modified=datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds"),
                )
            )
        return mods

    def set_mod_enabled(self, server_id: str, filename: str, enabled: bool) -> list[ModFile]:
        src = self._mod_path(server_id, filename)
        if not os.path.isfile(src):
            raise AdminError(f"{filename} not found")
        base = filename.removesuffix(".disabled")
        dest = self._mod_path(server_id, base if enabled else base + ".disabled")
        if src != dest:
            os.replace(src, dest)
        return self.list_mods(server_id)

    def delete_mod(self, server_id: str, filename: str) -> list[ModFile]:
        """Move a mod to .mcsm/removed-mods, like the builder does, so it can be restored."""
        src = self._mod_path(server_id, filename)
        if not os.path.isfile(src):
            raise AdminError(f"{filename} not found")
        dest_dir = self._path(server_id, ".mcsm", "removed-mods")
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(src, os.path.join(dest_dir, filename.removesuffix(".disabled")))
        return self.list_mods(server_id)

    def upload_mod(self, server_id: str, filename: str, data: bytes) -> list[ModFile]:
        if not filename.endswith(".jar"):
            raise AdminError("Only .jar files can be added")
        if len(data) > MAX_UPLOAD_BYTES:
            raise AdminError("The file is too big")
        if data[:2] != b"PK":
            raise AdminError("That file is not a valid .jar")
        dest = self._mod_path(server_id, filename)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        self._chown(os.path.dirname(dest))
        tmp = dest + ".mcsm-tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        self._chown(dest)
        return self.list_mods(server_id)
