"""Create and control one itzg/minecraft-server container per managed server.

Server metadata lives in container labels, so the Docker daemon is the source of
truth and nothing needs a separate database. Servers that are still pulling the
image or being created are tracked in memory until their container exists.
"""

from __future__ import annotations

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


class ServerNotFound(Exception):
    pass


class ServerConflict(Exception):
    pass


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40] or "server"


def _label(c, key: str, default: str = "") -> str:
    return (c.labels or {}).get(LABEL_PREFIX + key, default)


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
        try:
            me = self.client.containers.get(socket.gethostname())
            mounts = me.attrs.get("Mounts", [])
        except DockerException:
            return data_root
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

    def _host_data_path(self, server_id: str) -> str:
        return os.path.join(self.host_data_root, server_id)

    def _managed_containers(self):
        return self.client.containers.list(all=True, filters={"label": f"{MANAGED_LABEL}=true"})

    def _get_container(self, server_id: str):
        try:
            c = self.client.containers.get(self._container_name(server_id))
        except NotFound:
            return None
        if _label(c, "managed") != "true":
            return None
        return c

    @staticmethod
    def _status_of(c) -> tuple[str, str]:
        state = c.attrs.get("State", {})
        status = state.get("Status", c.status)
        health = (state.get("Health") or {}).get("Status")
        if status == "running":
            return ("running" if health in (None, "healthy") else "starting"), ""
        if status == "created":
            return "creating", ""
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

    def _info_from_container(self, c) -> ServerInfo:
        status, error = self._status_of(c)
        server_id = _label(c, "id")
        build = self.get_build(server_id)
        if build is not None:
            if build.phase in BUILD_ACTIVE_PHASES:
                status, error = ("installing" if build.phase == "installing" else "testing"), ""
            elif build.phase == "failed" and status not in ("running", "starting"):
                status, error = "error", build.message
        return ServerInfo(
            id=server_id,
            name=_label(c, "name", server_id),
            source=_label(c, "source"),
            project_id=_label(c, "project_id"),
            project_slug=_label(c, "project_slug"),
            version_id=_label(c, "version_id"),
            pack_title=_label(c, "pack_title"),
            minecraft_version=_label(c, "minecraft_version"),
            loader=_label(c, "loader"),
            port=int(_label(c, "port", "0") or 0),
            memory_gb=int(_label(c, "memory_gb", "0") or 0),
            java_version=_label(c, "java_version"),
            status=status,
            container_name=c.name,
            data_path=self._host_data_path(server_id),
            created_at=_label(c, "created_at"),
            error=error,
            build=build,
        )

    def _used_ports(self) -> dict[int, str]:
        used = {int(_label(c, "port", "0") or 0): _label(c, "name") for c in self._managed_containers()}
        with self._lock:
            used.update({p.port: p.name for p in self._pending.values()})
        return used

    def _unique_id(self, name: str) -> str:
        base = slugify(name)
        taken = {_label(c, "id") for c in self._managed_containers()}
        with self._lock:
            taken |= set(self._pending)
        candidate, n = base, 2
        while candidate in taken or os.path.exists(self._local_data_path(candidate)):
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    # ---- public API --------------------------------------------------------

    def list_servers(self) -> list[ServerInfo]:
        servers = {s.id: s for s in (self._info_from_container(c) for c in self._managed_containers())}
        with self._lock:
            for sid, info in self._pending.items():
                servers.setdefault(sid, info)
        return sorted(servers.values(), key=lambda s: s.created_at or "", reverse=True)

    def get_server(self, server_id: str) -> ServerInfo:
        c = self._get_container(server_id)
        if c is not None:
            return self._info_from_container(c)
        with self._lock:
            if server_id in self._pending:
                return self._pending[server_id]
        raise ServerNotFound(server_id)

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

    def mods_dir(self, server_id: str) -> str:
        return os.path.join(self._local_data_path(server_id), "mods")

    def local_data_path(self, server_id: str) -> str:
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
            if auto_fix:
                from .server_builder import ServerBuilder

                ServerBuilder(self).run(info, env)
            else:
                self.create_container(info, env)
        except (DockerException, OSError) as e:
            log.exception("Failed to create server %s", info.id)
            msg = getattr(e, "explanation", None) or str(e)
            self._set_pending(info.id, status="error", error=f"Could not create server: {msg}")

    def start_server(self, server_id: str) -> ServerInfo:
        c = self._require(server_id)
        # A failed build leaves the container without auto-restart; turn it back on.
        c.update(restart_policy={"Name": "unless-stopped"})
        c.start()
        c.reload()
        return self._info_from_container(c)

    def stop_server(self, server_id: str) -> ServerInfo:
        c = self._require(server_id)
        # Give the server time to save worlds before Docker kills it.
        c.stop(timeout=60)
        c.reload()
        return self._info_from_container(c)

    def restart_server(self, server_id: str) -> ServerInfo:
        c = self._require(server_id)
        c.restart(timeout=60)
        c.reload()
        return self._info_from_container(c)

    def delete_server(self, server_id: str, delete_data: bool = False) -> None:
        with self._lock:
            pending = self._pending.pop(server_id, None)
            if server_id in self._builds:
                self._cancelled.add(server_id)
                building = True
            else:
                building = False
        c = self._get_container(server_id)
        if c is None and pending is None and not building:
            raise ServerNotFound(server_id)
        if c is not None:
            c.remove(force=True)
        if delete_data:
            self._delete_data(server_id)

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

    def is_cancelled(self, server_id: str) -> bool:
        with self._lock:
            return server_id in self._cancelled

    def _require(self, server_id: str):
        if self.is_building(server_id):
            raise ServerConflict("The server is still being built and tested. Wait for it to finish.")
        c = self._get_container(server_id)
        if c is None:
            with self._lock:
                if server_id in self._pending:
                    raise ServerConflict("Server is still being set up")
            raise ServerNotFound(server_id)
        return c
