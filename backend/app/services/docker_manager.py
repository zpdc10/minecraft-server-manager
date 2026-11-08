"""Create and control one itzg/minecraft-server container per managed server.

Server metadata lives in container labels, so the Docker daemon is the source of
truth. A copy is also kept in each server's folder (.mcsm/server.json), so a server
whose container was deleted or lost (for example when Unraid's docker image is
rebuilt) still shows up and can be recreated. Servers that are still pulling the
image or being created are tracked in memory until their container exists.

Minecraft containers this app didn't create are discovered too ("ext-" ids). They
are read-only by default: the app can start, stop and inspect them, and use their
files when it can see them, but never recreates, renames or deletes them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import threading
from datetime import UTC, datetime

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from ..config import Settings
from ..models.server import BuildInfo, CreateServerRequest, ServerInfo
from .itzg_env import build_env, pick_java_version

log = logging.getLogger(__name__)

LABEL_PREFIX = "mcsm."
MANAGED_LABEL = f"{LABEL_PREFIX}managed"
MC_PORT = "25565/tcp"
# Exit codes that mean a clean shutdown (0 = graceful, 143 = SIGTERM).
CLEAN_EXIT_CODES = {0, 143}
BUILD_ACTIVE_PHASES = {"installing", "scanning", "testing"}
INFO_LABELS = (
    "id",
    "name",
    "source",
    "project_id",
    "project_slug",
    "version_id",
    "pack_title",
    "minecraft_version",
    "loader",
    "port",
    "memory_gb",
    "java_version",
    "created_at",
)


# Ids of discovered containers start with this, so they never clash with our own.
EXT_PREFIX = "ext-"
RECORD_FILE = "server.json"
FORGOTTEN_FILE = "server.forgotten.json"
# Env that create_container adds or that only applies to one build step.
OWN_ENV = {"UID", "GID", "SETUP_ONLY"}
# Secrets are not copied into the server folder; they are filled in again on recreate.
SECRET_ENV = {"CF_API_KEY"}
MC_PORTS = ("25565/tcp", "19132/udp")
_MC_IMAGE_RE = re.compile(
    r"minecraft|(^|[/_.-])(paper|papermc|purpur|spigot|bukkit|forge|neoforge|fabric|bedrock|nukkit|"
    r"pocketmine|folia|sponge|quilt)([/_.-]|$)",
    re.I,
)
# Minecraft tools that aren't servers, and this app itself.
_NOT_SERVER_RE = re.compile(r"mc-backup|mc-router|mc-monitor|mc-proxy|rcon|minecraft-server-manager", re.I)
_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*$", re.I)


class ServerNotFound(Exception):
    pass


class ServerConflict(Exception):
    pass


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40] or "server"


def _label(c, key: str, default: str = "") -> str:
    return (c.labels or {}).get(LABEL_PREFIX + key, default)


def env_dict(env_list: list[str] | None) -> dict[str, str]:
    env = {}
    for item in env_list or []:
        k, _, v = item.partition("=")
        env[k] = v
    return env


def _image_repo(image: str) -> str:
    """'docker.io/itzg/minecraft-server:java21' -> 'docker.io/itzg/minecraft-server'."""
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        image = image[: len(image) - len(last)] + last.split(":", 1)[0]
    return image.split("@", 1)[0]


def _image_tag(image: str) -> str:
    last = image.split("@", 1)[0].rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else "latest"


def looks_like_minecraft(attrs: dict) -> bool:
    """Whether a container (its `docker inspect` attrs) runs a Minecraft server."""
    config = attrs.get("Config") or {}
    repo = _image_repo((config.get("Image") or "").lower())
    if _NOT_SERVER_RE.search(repo):
        return False
    if _MC_IMAGE_RE.search(repo):
        return True
    if env_dict(config.get("Env")).get("EULA", "").strip().upper() == "TRUE":
        return True
    exposed = config.get("ExposedPorts") or {}
    bound = (attrs.get("HostConfig") or {}).get("PortBindings") or {}
    return any(p in exposed or p in bound for p in MC_PORTS)


def _memory_gb(env: dict[str, str], limit_bytes: int) -> int:
    for key in ("MEMORY", "MAX_MEMORY", "INIT_MEMORY"):
        m = _MEM_RE.match(env.get(key, ""))
        if m:
            factor = {"": 1 / 2**30, "k": 1 / 2**20, "m": 1 / 1024, "g": 1, "t": 1024}[m.group(2).lower()]
            return max(1, round(float(m.group(1)) * factor))
    return round(limit_bytes / 2**30) if limit_bytes else 0


class DockerManager:
    def __init__(self, settings: Settings, client: docker.DockerClient | None = None):
        self.settings = settings
        self._client = client
        self._pending: dict[str, ServerInfo] = {}
        self._lock = threading.Lock()
        self._host_root: str | None = None
        # Builds running in this process, and builds the user cancelled by deleting.
        self._builds: dict[str, BuildInfo] = {}
        self._cancelled: set[str] = set()
        # Servers in the middle of a long job (a pack update), with what is happening.
        self._busy: dict[str, str] = {}
        self._mounts: list[dict] | None = None
        self._self_id: str | None = None

    # ---- helpers -----------------------------------------------------------

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _container_name(self, server_id: str) -> str:
        return f"{self.settings.container_prefix}{server_id}"

    def _local_data_path(self, server_id: str) -> str:
        return os.path.join(self.settings.data_root, server_id)

    @property
    def host_data_root(self) -> str:
        """data_root as a path on the Docker host, for bind mounts."""
        if self._host_root is None:
            self._host_root = self.settings.host_data_root or self._detect_host_data_root()
        return self._host_root

    def _detect_host_data_root(self) -> str:
        """Find data_root's host path from this app's own container mounts.

        Inside a container the hostname is the container id, so we can look
        ourselves up. Falls back to data_root when not in a container.
        """
        data_root = os.path.normpath(self.settings.data_root)
        mounts = self._own_mounts()
        best = None
        for m in mounts:
            dest = os.path.normpath(m.get("Destination", ""))
            if data_root == dest or data_root.startswith(dest + "/"):
                if best is None or len(dest) > len(best[0]):
                    best = (dest, m.get("Source", ""))
        if best and best[1]:
            return best[1] + data_root[len(best[0]) :]
        log.warning("Could not find the host path for %s; set HOST_DATA_ROOT", data_root)
        return data_root

    def _own_mounts(self) -> list[dict]:
        """This app's own container mounts (empty when not running in a container)."""
        if self._mounts is None:
            try:
                me = self.client.containers.get(socket.gethostname())
                self._mounts = list(me.attrs.get("Mounts") or [])
                self._self_id = getattr(me, "id", None) or socket.gethostname()
            except DockerException:
                self._mounts = []
        return self._mounts

    def _host_to_local(self, host_path: str) -> str:
        """Where a host path can be read inside this app's container, or "" if it can't."""
        if not host_path:
            return ""
        host_path = os.path.normpath(host_path)
        pairs = [(m.get("Source", ""), m.get("Destination", "")) for m in self._own_mounts()]
        pairs.append((self.host_data_root, self.settings.data_root))
        best = None
        for src, dest in pairs:
            if not src or not dest:
                continue
            src = os.path.normpath(src)
            if host_path == src or host_path.startswith(src.rstrip("/") + "/"):
                if best is None or len(src) > len(best[0]):
                    best = (src, dest)
        if best is None:
            # Not in a container (or the same paths inside and out).
            return host_path if not self._own_mounts() and os.path.isdir(host_path) else ""
        return os.path.normpath(best[1] + host_path[len(best[0]) :])

    def _host_data_path(self, server_id: str) -> str:
        return os.path.join(self.host_data_root, server_id)

    def _all_containers(self):
        return self.client.containers.list(all=True)

    def _managed_containers(self):
        return self.client.containers.list(all=True, filters={"label": f"{MANAGED_LABEL}=true"})

    def _is_self(self, c) -> bool:
        me = socket.gethostname()
        cid = getattr(c, "id", None) or ""
        return c.name == me or (bool(cid) and cid.startswith(me)) or cid == self._self_id

    def _external(self) -> dict[str, object]:
        """Minecraft containers this app didn't create, by their ext- id."""
        found: dict[str, object] = {}
        for c in sorted(self._all_containers(), key=lambda c: c.name):
            if _label(c, "managed") == "true" or self._is_self(c):
                continue
            if c.name.startswith(self.settings.container_prefix) and self._load_record(
                c.name[len(self.settings.container_prefix) :]
            ):
                continue  # one of ours that lost its labels; handled as managed
            try:
                if not looks_like_minecraft(c.attrs):
                    continue
            except (AttributeError, KeyError, TypeError):
                continue
            base = EXT_PREFIX + slugify(c.name)
            sid, n = base, 2
            while sid in found:
                sid, n = f"{base}-{n}", n + 1
            found[sid] = c
        return found

    def is_managed(self, server_id: str) -> bool:
        return not server_id.startswith(EXT_PREFIX)

    def _get_container(self, server_id: str):
        if server_id.startswith(EXT_PREFIX):
            return self._external().get(server_id)
        try:
            c = self.client.containers.get(self._container_name(server_id))
        except NotFound:
            return None
        if _label(c, "managed") != "true" and self._load_record(server_id) is None:
            return None
        return c

    # ---- saved server records (survive the container being deleted) ----------

    def _record_path(self, server_id: str, name: str = RECORD_FILE) -> str:
        return os.path.join(self._local_data_path(server_id), ".mcsm", name)

    def save_record(self, info: ServerInfo, env: dict[str, str]) -> None:
        record = {
            "info": {k: getattr(info, k) for k in INFO_LABELS},
            "env": {k: v for k, v in env.items() if k not in OWN_ENV | SECRET_ENV},
        }
        path = self._record_path(info.id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(record, f, indent=1)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("Could not save the server record for %s: %s", info.id, e)

    def _load_record(self, server_id: str) -> dict | None:
        if not server_id or "/" in server_id or server_id.startswith((".", EXT_PREFIX)):
            return None
        try:
            with open(self._record_path(server_id)) as f:
                record = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or not isinstance(record.get("info"), dict):
            return None
        return record

    def _forget_record(self, server_id: str) -> None:
        """Stop listing a deleted server whose files were kept."""
        try:
            os.replace(self._record_path(server_id), self._record_path(server_id, FORGOTTEN_FILE))
        except OSError:
            pass

    def _record_ids(self) -> list[str]:
        try:
            names = os.listdir(self.settings.data_root)
        except OSError:
            return []
        return [n for n in names if os.path.isfile(self._record_path(n))]

    def _info_from_record(self, server_id: str, record: dict) -> ServerInfo:
        data = {k: record["info"].get(k) for k in INFO_LABELS if record["info"].get(k) is not None}
        data["id"] = server_id
        data.setdefault("name", server_id)
        data.setdefault("source", "")
        data["port"] = int(data.get("port") or 0)
        data["memory_gb"] = int(data.get("memory_gb") or 0)
        data.setdefault("java_version", "latest")
        return ServerInfo(
            **data,
            status="missing",
            container_name=self._container_name(server_id),
            data_path=self._host_data_path(server_id),
            error="The server's container is gone, but its world and files are still here. "
            "Recreate it to bring the server back.",
        )

    def user_env(self, c) -> dict[str, str]:
        """The container's env minus what the image and create_container add."""
        env = env_dict((c.attrs.get("Config") or {}).get("Env"))
        image_env: dict[str, str] = {}
        try:
            image = (c.attrs.get("Config") or {}).get("Image") or c.attrs.get("Image")
            if image:
                image_env = env_dict(self.client.images.get(image).attrs.get("Config", {}).get("Env"))
        except (ImageNotFound, DockerException, AttributeError):
            pass
        return {k: v for k, v in env.items() if k not in OWN_ENV and image_env.get(k) != v}

    @staticmethod
    def _status_of(c) -> tuple[str, str]:
        state = c.attrs.get("State", {})
        status = state.get("Status", c.status)
        health = (state.get("Health") or {}).get("Status")
        if status == "running":
            return ("running" if health in (None, "healthy") else "starting"), ""
        if status == "created":
            # Created but never started: a stopped server whose settings were changed.
            return "stopped", ""
        if status == "restarting":
            return "error", "Server keeps crashing and restarting. Check the logs."
        if status == "removing":
            return "stopping", ""
        if status in ("exited", "dead"):
            code = state.get("ExitCode", 0)
            if code in CLEAN_EXIT_CODES:
                return "stopped", ""
            return "error", f"Server exited with code {code}. Check the logs."
        if status == "paused":
            return "stopped", ""
        return "unknown", ""

    # ---- build reports ---------------------------------------------------------

    def build_report_path(self, server_id: str) -> str:
        return os.path.join(self._local_data_path(server_id), ".mcsm", "build.json")

    def save_build(self, server_id: str, build: BuildInfo) -> None:
        with self._lock:
            self._builds[server_id] = build
        path = self.build_report_path(server_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(build.model_dump_json(indent=1))
            os.replace(tmp, path)
        except OSError as e:
            log.warning("Could not save build report for %s: %s", server_id, e)

    def finish_build(self, server_id: str, build: BuildInfo) -> None:
        self.save_build(server_id, build)
        with self._lock:
            self._builds.pop(server_id, None)

    def get_build(self, server_id: str) -> BuildInfo | None:
        with self._lock:
            if server_id in self._builds:
                return self._builds[server_id]
        try:
            with open(self.build_report_path(server_id)) as f:
                build = BuildInfo.model_validate_json(f.read())
        except (OSError, ValueError):
            return None
        if build.phase in BUILD_ACTIVE_PHASES:
            # The build thread is gone: the app restarted in the middle of it.
            build = build.model_copy(
                update={
                    "phase": "failed",
                    "message": "The app restarted during the build. Start the server to try it, "
                    "or delete it and create it again.",
                }
            )
        return build

    def is_building(self, server_id: str) -> bool:
        with self._lock:
            b = self._builds.get(server_id)
        return b is not None and b.phase in BUILD_ACTIVE_PHASES

    def _info_from_container(self, c, server_id: str | None = None) -> ServerInfo:
        if server_id and server_id.startswith(EXT_PREFIX):
            return self._external_info(server_id, c)
        status, error = self._status_of(c)
        server_id = _label(c, "id") or server_id or c.name.removeprefix(self.settings.container_prefix)
        build = self.get_build(server_id)
        if build is not None:
            if build.phase in BUILD_ACTIVE_PHASES:
                status, error = ("installing" if build.phase == "installing" else "testing"), ""
            elif build.phase == "failed" and status not in ("running", "starting"):
                status, error = "error", build.message
        if _label(c, "managed") == "true":
            meta = {k: _label(c, k) for k in INFO_LABELS}
        else:
            # Our container, recreated outside the app without its labels.
            meta = {k: str(v) for k, v in ((self._load_record(server_id) or {}).get("info") or {}).items()}
        return ServerInfo(
            id=server_id,
            name=meta.get("name") or server_id,
            source=meta.get("source", ""),
            project_id=meta.get("project_id", ""),
            project_slug=meta.get("project_slug", ""),
            version_id=meta.get("version_id", ""),
            pack_title=meta.get("pack_title", ""),
            minecraft_version=meta.get("minecraft_version", ""),
            loader=meta.get("loader", ""),
            port=int(meta.get("port") or 0),
            memory_gb=int(meta.get("memory_gb") or 0),
            java_version=meta.get("java_version", ""),
            status=status,
            container_name=c.name,
            data_path=self._host_data_path(server_id),
            created_at=meta.get("created_at", ""),
            error=error,
            build=build,
            busy=self.busy(server_id),
        )

    def _external_mount(self, c) -> str:
        """Host folder holding a discovered server's files."""
        mounts = [m for m in (c.attrs.get("Mounts") or []) if m.get("Source")]
        for m in mounts:
            if m.get("Destination") == "/data":
                return m["Source"]
        for word in ("data", "minecraft", "server", "world"):
            for m in mounts:
                if word in (m.get("Destination") or "").lower() and m.get("Type", "bind") in ("bind", "volume"):
                    return m["Source"]
        return mounts[0]["Source"] if mounts else ""

    @staticmethod
    def _external_port(c, env: dict[str, str]) -> int:
        attrs = c.attrs
        bindings = dict((attrs.get("HostConfig") or {}).get("PortBindings") or {})
        bindings.update({k: v for k, v in ((attrs.get("NetworkSettings") or {}).get("Ports") or {}).items() if v})
        for key in (*MC_PORTS, *sorted(bindings)):
            for b in bindings.get(key) or []:
                if str(b.get("HostPort", "")).isdigit():
                    return int(b["HostPort"])
        if (attrs.get("HostConfig") or {}).get("NetworkMode") == "host":
            port = env.get("SERVER_PORT", "")
            return int(port) if port.isdigit() else 25565
        return 0

    def _external_info(self, server_id: str, c) -> ServerInfo:
        attrs = c.attrs
        config = attrs.get("Config") or {}
        env = env_dict(config.get("Env"))
        image = config.get("Image") or ""
        status, error = self._status_of(c)
        mc_type = env.get("TYPE", "").upper()
        pack = (
            env.get("CF_SLUG")
            or env.get("MODRINTH_MODPACK")
            or env.get("MODRINTH_PROJECT")
            or env.get("FTB_MODPACK_ID")
            or env.get("CF_PAGE_URL", "").rstrip("/").rsplit("/", 1)[-1]
        )
        loader = env.get("MODRINTH_LOADER", "").lower() or (
            mc_type.lower() if mc_type and not mc_type.startswith(("AUTO_", "MODRINTH", "CURSEFORGE", "FTB")) else ""
        )
        version = env.get("VERSION", "")
        host_path = self._external_mount(c)
        local = self._host_to_local(host_path)
        files = bool(local) and os.path.isdir(local)
        note = ""
        if not host_path:
            note = "This container has no data folder mounted, so world backups and file tools are unavailable."
        elif not files:
            note = (
                f"The app can't see this server's files ({host_path}). To use backups, mods and "
                f"server.properties, add a path to the manager container that maps {host_path} "
                "to the same path inside it."
            )
        return ServerInfo(
            id=server_id,
            name=c.name,
            source="external",
            pack_title=pack or (mc_type.title() if mc_type else ""),
            minecraft_version="" if version.upper() == "LATEST" else version,
            loader=loader,
            port=self._external_port(c, env),
            memory_gb=_memory_gb(env, (attrs.get("HostConfig") or {}).get("Memory") or 0),
            java_version=_image_tag(image),
            status=status,
            container_name=c.name,
            data_path=host_path,
            created_at=(attrs.get("Created") or "")[:19],
            error=error,
            managed=False,
            image=image,
            files_available=files,
            note=note,
        )

    def _used_ports(self, exclude: str | None = None) -> dict[int, str]:
        """Host ports taken by any Minecraft server, ours or not."""
        used: dict[int, str] = {}
        for s in self.list_servers():
            if s.port and s.id != exclude:
                used.setdefault(s.port, s.name)
        return used

    def _unique_id(self, name: str) -> str:
        base = slugify(name)
        if base.startswith(EXT_PREFIX.rstrip("-")):
            base = "server-" + base
        taken = {_label(c, "id") for c in self._managed_containers()} | set(self._record_ids())
        with self._lock:
            taken |= set(self._pending)
        candidate, n = base, 2
        while candidate in taken or os.path.exists(self._local_data_path(candidate)):
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    # ---- public API --------------------------------------------------------

    def list_servers(self) -> list[ServerInfo]:
        """Every Minecraft server: ours (running or not), ours whose container is gone,
        and Minecraft containers found on the Docker host that we didn't create."""
        servers: dict[str, ServerInfo] = {}
        for c in self._managed_containers():
            info = self._info_from_container(c)
            servers[info.id] = info
            if self._load_record(info.id) is None and not self.is_building(info.id):
                # Servers made before records existed: save one now.
                self.save_record(info, self.user_env(c))
        prefix = self.settings.container_prefix
        with self._lock:
            busy = set(self._pending) | set(self._builds)
        for sid in self._record_ids():
            if sid in servers or sid in busy:
                continue
            try:
                c = self.client.containers.get(prefix + sid)
            except NotFound:
                c = None
            if c is not None and not getattr(c, "removed", False):
                servers[sid] = self._info_from_container(c, sid)
            else:
                record = self._load_record(sid)
                if record:
                    servers[sid] = self._info_from_record(sid, record)
        for sid, c in self._external().items():
            servers[sid] = self._external_info(sid, c)
        with self._lock:
            for sid, info in self._pending.items():
                servers.setdefault(sid, info)
        # Ours first (newest first), then the ones found on the host.
        ordered = sorted(servers.values(), key=lambda s: s.created_at or "", reverse=True)
        return sorted(ordered, key=lambda s: not s.managed)

    def get_server(self, server_id: str) -> ServerInfo:
        c = self._get_container(server_id)
        if c is not None:
            return self._info_from_container(c, server_id)
        with self._lock:
            if server_id in self._pending:
                return self._pending[server_id]
        record = self._load_record(server_id)
        if record is not None and not self.is_building(server_id):
            return self._info_from_record(server_id, record)
        raise ServerNotFound(server_id)

    def recreate_server(self, server_id: str, background: bool = True) -> ServerInfo:
        """Make a new container for a server whose container is gone, from its saved record."""
        if not self.is_managed(server_id):
            raise ServerConflict("This server wasn't created by the app, so it can't be recreated here.")
        if self._get_container(server_id) is not None:
            raise ServerConflict("The server already has a container.")
        with self._lock:
            if server_id in self._pending:
                raise ServerConflict("The server is already being set up.")
        record = self._load_record(server_id)
        if record is None:
            raise ServerNotFound(server_id)
        info = self._info_from_record(server_id, record)
        used = self._used_ports(exclude=server_id)
        if info.port in used:
            raise ServerConflict(f"Port {info.port} is now used by '{used[info.port]}'. Free it first.")
        env = dict(record.get("env") or {})
        if info.source == "curseforge":
            from .. import settings_store

            env["CF_API_KEY"] = settings_store.curseforge_api_key()
        info = info.model_copy(update={"status": "pulling", "error": ""})
        with self._lock:
            self._pending[server_id] = info

        def run():
            try:
                self.pull_image(info.java_version)
                self._set_pending(server_id, status="creating")
                self.create_container(info, env)
            except (DockerException, OSError) as e:
                log.exception("Failed to recreate server %s", server_id)
                msg = getattr(e, "explanation", None) or str(e)
                self._set_pending(server_id, status="error", error=f"Could not recreate the container: {msg}")

        if background:
            threading.Thread(target=run, daemon=True).start()
        else:
            run()
        return info

    def create_server(self, req: CreateServerRequest, background: bool = True) -> ServerInfo:
        """Validate the request, then pull the image and start the container.

        Raises EnvBuildError / ServerConflict for bad input, before anything is
        created. The slow part (image pull) runs in a background thread.
        """
        env = build_env(req, self.settings)  # fail fast on bad config
        used = self._used_ports()
        if req.port in used:
            raise ServerConflict(f"Port {req.port} is already used by server '{used[req.port]}'")

        server_id = self._unique_id(req.name)
        java = req.java_version or pick_java_version(req.minecraft_version)
        info = ServerInfo(
            id=server_id,
            name=req.name,
            source=req.source,
            project_id=req.project_id,
            project_slug=req.project_slug,
            version_id=req.version_id,
            pack_title=req.pack_title,
            minecraft_version=req.minecraft_version,
            loader=req.loader,
            port=req.port,
            memory_gb=req.memory_gb,
            java_version=java,
            status="pulling",
            container_name=self._container_name(server_id),
            data_path=self._host_data_path(server_id),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._pending[server_id] = info

        auto_fix = req.auto_fix and req.source != "vanilla"
        if background:
            threading.Thread(target=self._provision, args=(info, env, auto_fix), daemon=True).start()
        else:
            self._provision(info, env, auto_fix)
        return info

    def _set_pending(self, server_id: str, **changes) -> None:
        with self._lock:
            if server_id in self._pending:
                self._pending[server_id] = self._pending[server_id].model_copy(update=changes)

    def pull_image(self, java_version: str) -> str:
        image = f"{self.settings.mc_image}:{java_version}"
        try:
            self.client.images.get(image)
        except ImageNotFound:
            log.info("Pulling %s", image)
            self.client.images.pull(self.settings.mc_image, tag=java_version)
        return image

    def file_owner(self, server_id: str) -> tuple[int, int]:
        """uid/gid new files in the server folder should get: whoever owns the folder."""
        try:
            st = os.stat(self.local_data_path(server_id))
            if st.st_uid or st.st_gid:
                return st.st_uid, st.st_gid
        except OSError:
            pass
        return self.settings.mc_uid, self.settings.mc_gid

    def mods_dir(self, server_id: str) -> str:
        return os.path.join(self._local_data_path(server_id), "mods")

    def local_data_path(self, server_id: str) -> str:
        """The server's folder inside this app's container ("" for a discovered server
        whose files the app can't see)."""
        if server_id.startswith(EXT_PREFIX):
            c = self._external().get(server_id)
            return self._host_to_local(self._external_mount(c)) if c is not None else ""
        return self._local_data_path(server_id)

    def create_container(self, info: ServerInfo, env: dict[str, str], restart: bool = True, start: bool = True):
        """(Re)create the server's container from info and env, replacing any old one."""
        old = self._get_container(info.id)
        if old is not None:
            old.remove(force=True)
        env = {**env, "UID": str(self.settings.mc_uid), "GID": str(self.settings.mc_gid)}
        labels = {MANAGED_LABEL: "true", **{LABEL_PREFIX + k: str(getattr(info, k)) for k in INFO_LABELS}}
        # JVM needs room beyond the heap; give it 25% (min 1 GB) of headroom.
        overhead_mb = max(1024, info.memory_gb * 256)
        kwargs = dict(
            image=f"{self.settings.mc_image}:{info.java_version}",
            name=info.container_name,
            environment=env,
            labels=labels,
            ports={MC_PORT: info.port},
            volumes={info.data_path: {"bind": "/data", "mode": "rw"}},
            mem_limit=f"{info.memory_gb * 1024 + overhead_mb}m",
            restart_policy={"Name": "unless-stopped" if restart else "no"},
            stdin_open=True,
            tty=True,
            detach=True,
        )
        if self.settings.docker_network:
            kwargs["network"] = self.settings.docker_network
        container = self.client.containers.create(**kwargs)
        self.save_record(info, env)
        if start:
            try:
                container.start()
            except APIError as e:
                container.remove(force=True)
                raise e
        with self._lock:
            self._pending.pop(info.id, None)
        return container

    def _provision(self, info: ServerInfo, env: dict[str, str], auto_fix: bool = False) -> None:
        try:
            self.pull_image(info.java_version)
            self._set_pending(info.id, status="creating")
            os.makedirs(self._local_data_path(info.id), exist_ok=True)
            try:
                os.chown(self._local_data_path(info.id), self.settings.mc_uid, self.settings.mc_gid)
            except (PermissionError, OSError):
                pass  # not fatal: itzg fixes ownership of /data on start when run as root
            build = None
            if info.source == "custom":
                self._install_custom_pack(info)
            elif info.source == "cfimport":
                env, build = self._install_cf_import(info, env)
                if build is None:
                    return
            if auto_fix:
                from .server_builder import ServerBuilder

                ServerBuilder(self).run(info, env, build)
            else:
                if build is not None:
                    build.phase, build.message = "passed", "Pack downloaded; the server is starting without a test run."
                    build.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
                    self.finish_build(info.id, build)
                self.create_container(info, env)
        except (DockerException, OSError, RuntimeError) as e:
            log.exception("Failed to create server %s", info.id)
            msg = getattr(e, "explanation", None) or str(e)
            self._set_pending(info.id, status="error", error=f"Could not create server: {msg}")

    def _install_custom_pack(self, info: ServerInfo) -> None:
        """Copy a pack-builder pack's server mods into the server folder before it starts."""
        from . import pack_install, pack_store

        try:
            pack = pack_store.get(info.project_id)
        except pack_store.PackNotFound as e:
            raise RuntimeError(f"Custom pack '{info.project_id}' was deleted") from e
        notes = pack_install.install(pack, self._local_data_path(info.id), self.settings.mc_uid, self.settings.mc_gid)
        for note in notes:
            log.info("Server %s: %s", info.id, note)

    def _install_cf_import(self, info: ServerInfo, env: dict[str, str]):
        """Download an imported CurseForge pack into the server folder.

        Returns (env with the loader settings, the build record so far), or
        (env, None) when the download failed and the server was marked as errored.
        """
        from . import cf_import

        build = BuildInfo(
            phase="installing",
            message="Downloading the modpack",
            max_attempts=self.settings.build_max_attempts,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

        def progress(message: str) -> None:
            build.message = message
            self._set_pending(info.id, status="installing", build=build.model_copy())
            self.save_build(info.id, build)

        progress("Downloading the modpack")
        try:
            loader_env, notes, _ = cf_import.install(
                info.project_id,
                info.version_id,
                self._local_data_path(info.id),
                self.settings.mc_uid,
                self.settings.mc_gid,
                progress=progress,
            )
        except cf_import.PackImportError as e:
            build.phase, build.message = "failed", f"Importing the pack failed: {e}"
            build.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
            self.finish_build(info.id, build)
            self._set_pending(info.id, status="error", error=build.message, build=build)
            return env, None
        build.notes = notes
        self.save_build(info.id, build)
        # The user's own extra variables still win over the pack's loader settings.
        return {**env, **{k: v for k, v in loader_env.items() if k not in env}}, build

    def start_server(self, server_id: str) -> ServerInfo:
        c = self._require(server_id)
        if self.is_managed(server_id):
            # A failed build leaves the container without auto-restart; turn it back on.
            c.update(restart_policy={"Name": "unless-stopped"})
        c.start()
        c.reload()
        return self._info_from_container(c, server_id)

    def stop_server(self, server_id: str) -> ServerInfo:
        c = self._require(server_id)
        # Give the server time to save worlds before Docker kills it.
        c.stop(timeout=60)
        c.reload()
        return self._info_from_container(c, server_id)

    def restart_server(self, server_id: str) -> ServerInfo:
        c = self._require(server_id)
        c.restart(timeout=60)
        c.reload()
        return self._info_from_container(c, server_id)

    def delete_server(self, server_id: str, delete_data: bool = False) -> None:
        if not self.is_managed(server_id):
            raise ServerConflict(
                "This server wasn't created by the app, so the app won't delete it. Remove it from Unraid's Docker tab."
            )
        if self.busy(server_id):
            raise ServerConflict("The server is being updated. Wait for the update to finish, then delete it.")
        with self._lock:
            pending = self._pending.pop(server_id, None)
            if server_id in self._builds:
                self._cancelled.add(server_id)
                building = True
            else:
                building = False
        c = self._get_container(server_id)
        record = self._load_record(server_id)
        if c is None and pending is None and not building and record is None:
            raise ServerNotFound(server_id)
        if c is not None:
            c.remove(force=True)
        if delete_data:
            self._delete_data(server_id)
        else:
            self._forget_record(server_id)

    def _delete_data(self, server_id: str) -> None:
        root = os.path.realpath(self.settings.data_root)
        path = os.path.realpath(self._local_data_path(server_id))
        if os.path.dirname(path) != root or not server_id or "/" in server_id:
            raise ValueError(f"Refusing to delete unexpected path {path}")
        if os.path.isdir(path):
            shutil.rmtree(path)

    def get_logs(self, server_id: str, tail: int = 200) -> str:
        c = self._get_container(server_id)
        if c is None:
            info = self.get_server(server_id)  # raises if unknown
            return info.error or "Server is still being set up..."
        return c.logs(tail=tail).decode("utf-8", errors="replace")

    def set_busy(self, server_id: str, message: str | None) -> None:
        """Mark a server as in the middle of a job (None clears it). Start/stop/delete wait for it."""
        with self._lock:
            if message is None:
                self._busy.pop(server_id, None)
            else:
                self._busy[server_id] = message

    def busy(self, server_id: str) -> str:
        with self._lock:
            return self._busy.get(server_id, "")

    def is_cancelled(self, server_id: str) -> bool:
        with self._lock:
            return server_id in self._cancelled

    def _require(self, server_id: str):
        if self.busy(server_id):
            raise ServerConflict("The server is being updated. Wait for the update to finish.")
        if self.is_building(server_id):
            raise ServerConflict("The server is still being built and tested. Wait for it to finish.")
        c = self._get_container(server_id)
        if c is None:
            with self._lock:
                if server_id in self._pending:
                    raise ServerConflict("Server is still being set up")
            if self._load_record(server_id) is not None:
                raise ServerConflict("The server's container is gone. Recreate it first.")
            raise ServerNotFound(server_id)
        return c
