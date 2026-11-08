"""Interactive shell inside a server's container (docker exec with a TTY).

The shell starts in /data, the pack root (the folder holding /config, /mods and the
world). A running server is exec'd into directly. A stopped server gets a short-lived
helper container from the same image that shares the server's volumes, so scripts
that must run while the server is off (pack mode switchers, config fixes) work too.
The helper is removed when the shell closes.

There is no login in this app, so the websocket only accepts LAN clients from pages
served by the app itself (see `request_allowed`).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from docker.errors import NotFound

from .docker_manager import LABEL_PREFIX, DockerManager, ServerConflict, ServerNotFound

log = logging.getLogger(__name__)

WORKDIR = "/data"
SHELL_LABEL = f"{LABEL_PREFIX}shell_for"
# bash when the image has it (itzg does), plain sh otherwise.
SHELL_CMD = ["/bin/sh", "-c", "if command -v bash >/dev/null 2>&1; then exec bash -l; else exec sh -l; fi"]
SHELL_ENV = {
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
    # Keep shell history and dotfiles out of the pack folder.
    "HOME": "/tmp",
    "HISTFILE": "/tmp/.mcsm_history",
    "PS1": r"\u@mcsm:\w\$ ",
}
# Headers a reverse proxy or Cloudflare tunnel adds; their presence means the request
# did not come straight from the LAN.
FORWARD_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded")


class ShellError(Exception):
    pass


def _is_lan(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost", "testclient")
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def request_allowed(client_host: str | None, headers) -> tuple[bool, str]:
    """Only LAN clients, not via a proxy/tunnel, and only from this app's own pages."""
    if not _is_lan(client_host):
        return False, "The shell is only available from your local network."
    for h in FORWARD_HEADERS:
        value = headers.get(h)
        if value is None:
            continue
        hops = [p.strip().split(";")[0].removeprefix("for=").strip('"[]') for p in value.split(",")]
        if h == "forwarded" or not all(_is_lan(p) for p in hops if p):
            return False, "The shell can't be used through a proxy or tunnel."
    origin = headers.get("origin")
    if origin:
        # Blocks other websites in your browser from opening a shell (cross-site websockets).
        if urlsplit(origin).netloc.lower() != (headers.get("host") or "").lower():
            return False, "The shell only accepts connections from the manager's own page."
    return True, ""


@dataclass
class ShellSession:
    mgr: DockerManager
    server_id: str
    exec_id: str = ""
    helper: object | None = None
    sock: object | None = None
    raw: socket.socket | None = None
    note: str = ""
    _closed: bool = field(default=False)

    @property
    def api(self):
        return self.mgr.client.api

    def open(self, root: bool = False, cols: int = 80, rows: int = 24) -> None:
        c = self.mgr._get_container(self.server_id)
        if c is None:
            # get_server raises ServerNotFound for unknown ids.
            self.mgr.get_server(self.server_id)
            raise ShellError("The server has no container yet. Wait for it to be created, or recreate it.")
        c.reload()
        target = c
        if c.status != "running":
            target = self._start_helper(c)
            self.note = (
                "The server is stopped, so this shell runs in a helper container that shares its files. "
                "It is removed when you close the shell."
            )
        user = "0:0" if root else self._data_owner(target)
        created = self.api.exec_create(
            target.id,
            SHELL_CMD,
            stdin=True,
            tty=True,
            workdir=WORKDIR,
            environment=SHELL_ENV,
            user=user,
        )
        self.exec_id = created["Id"]
        self.sock = self.api.exec_start(self.exec_id, tty=True, socket=True)
        self.raw = getattr(self.sock, "_sock", self.sock)
        self.resize(cols, rows)

    def _data_owner(self, target) -> str:
        """Run as whoever owns /data so new files keep the server's ownership."""
        try:
            code, out = target.exec_run(["stat", "-c", "%u:%g", WORKDIR], user="0:0")
            owner = out.decode().strip() if code == 0 else ""
        except Exception:  # noqa: BLE001
            owner = ""
        if owner and owner.replace(":", "").isdigit():
            return owner
        uid, gid = self.mgr.file_owner(self.server_id)
        return f"{uid}:{gid}"

    def _start_helper(self, c):
        name = f"{c.name}_shell"
        try:
            self.mgr.client.containers.get(name).remove(force=True)
        except NotFound:
            pass
        image = (c.attrs.get("Config") or {}).get("Image") or c.image.id
        kwargs = dict(
            image=image,
            name=name,
            entrypoint=["sleep", "infinity"],
            labels={SHELL_LABEL: self.server_id},
            volumes_from=[c.id],
            working_dir=WORKDIR,
            detach=True,
            auto_remove=False,
        )
        network = (c.attrs.get("HostConfig") or {}).get("NetworkMode")
        if network and not network.startswith("container:"):
            kwargs["network_mode"] = network
        self.helper = self.mgr.client.containers.run(**kwargs)
        self.helper.reload()
        return self.helper

    def resize(self, cols: int, rows: int) -> None:
        if not self.exec_id or cols <= 0 or rows <= 0:
            return
        try:
            self.api.exec_resize(self.exec_id, height=rows, width=cols)
        except Exception:  # noqa: BLE001  (resizing an exited exec is harmless)
            pass

    def read(self) -> bytes:
        try:
            return self.raw.recv(65536)
        except OSError:
            return b""

    def write(self, data: bytes) -> None:
        if data:
            self.raw.sendall(data)

    def exit_code(self) -> int | None:
        try:
            return self.api.exec_inspect(self.exec_id).get("ExitCode")
        except Exception:  # noqa: BLE001
            return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.raw is not None:
            try:
                self.raw.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except Exception:  # noqa: BLE001
                pass
        if self.helper is not None:
            try:
                self.helper.remove(force=True)
            except Exception as e:  # noqa: BLE001
                log.warning("Couldn't remove shell helper for %s: %s", self.server_id, e)


def open_shell(mgr: DockerManager, server_id: str, root: bool, cols: int, rows: int) -> ShellSession:
    if mgr.busy(server_id):
        raise ShellError("The server is being updated. Wait for the update to finish.")
    session = ShellSession(mgr, server_id)
    try:
        session.open(root=root, cols=cols, rows=rows)
    except (ShellError, ServerNotFound, ServerConflict):
        session.close()
        raise
    except Exception as e:
        session.close()
        raise ShellError(f"Couldn't open a shell: {e}") from e
    return session


def cleanup_helpers(mgr: DockerManager) -> None:
    """Remove helper containers left behind if the app restarted mid-shell."""
    try:
        for c in mgr.client.containers.list(all=True, filters={"label": SHELL_LABEL}):
            c.remove(force=True)
    except Exception as e:  # noqa: BLE001
        log.debug("Shell helper cleanup skipped: %s", e)
