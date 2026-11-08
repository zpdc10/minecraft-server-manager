"""Tests for the server shell (websocket docker exec) and pack mode switching."""
# ruff: noqa: F811  (pytest fixtures imported from test_servers)

import json
import os
import socket
import threading

import pytest
from starlette.websockets import WebSocketDisconnect

from app.services import pack_modes
from app.services.docker_manager import looks_like_minecraft
from app.services.shell import SHELL_LABEL, request_allowed
from tests.test_servers import (  # noqa: F401  (fixtures)
    FakeContainer,
    client,
    docker_fake,
    mgr,
    modrinth_req,
    settings,
)


class FakeExecApi:
    """docker.APIClient's exec calls; the 'shell' echoes its input back."""

    def __init__(self):
        self.created = []
        self.resized = []
        self.peers = []

    def exec_create(self, container_id, cmd, **kwargs):
        self.created.append({"container": container_id, "cmd": cmd, **kwargs})
        return {"Id": f"exec{len(self.created)}"}

    def exec_start(self, exec_id, tty=False, socket=False):
        ours, theirs = _socketpair()
        self.peers.append(theirs)

        def shell():
            theirs.sendall(b"$ ")
            while True:
                data = theirs.recv(1024)
                if not data or data == b"exit\r":
                    break
                theirs.sendall(b"echo:" + data)
            theirs.close()

        threading.Thread(target=shell, daemon=True).start()
        return ours

    def exec_resize(self, exec_id, height, width):
        self.resized.append((width, height))

    def exec_inspect(self, exec_id):
        return {"ExitCode": 0}


def _socketpair():
    return socket.socketpair()


class ShellContainer(FakeContainer):
    def __init__(self, name, labels, **kwargs):
        super().__init__(name, labels, **kwargs)
        self.id = f"id-{name}"

    @property
    def attrs(self):
        a = super().attrs
        a["Config"] = {"Image": self.kwargs.get("image", "itzg/minecraft-server:java17"), "Labels": self.labels}
        a["HostConfig"] = {"NetworkMode": "bridge"}
        return a

    def exec_run(self, cmd, user=None):
        return 0, b"1000:100\n"


@pytest.fixture
def shell_server(client, docker_fake, tmp_path):
    docker_fake.api = FakeExecApi()

    def create(image, name, labels, **kwargs):
        c = ShellContainer(name, labels, image=image, **kwargs)
        docker_fake.containers.items[name] = c
        return c

    def run(image, name, labels, **kwargs):
        c = create(image, name, labels, **kwargs)
        c.status = "running"
        return c

    docker_fake.containers.create = create
    docker_fake.containers.run = run
    r = client.post("/api/servers", json=modrinth_req())
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _read_until(ws, needle: bytes) -> bytes:
    buf = b""
    while needle not in buf:
        msg = ws.receive()
        if msg.get("bytes"):
            buf += msg["bytes"]
        elif msg.get("type") == "websocket.close":
            break
    return buf


def test_shell_execs_into_running_server(client, docker_fake, shell_server):
    with client.websocket_connect(f"/api/servers/{shell_server}/shell?cols=100&rows=30") as ws:
        assert b"$ " in _read_until(ws, b"$ ")
        ws.send_text(json.dumps({"t": "i", "d": "ls"}))
        assert b"echo:ls" in _read_until(ws, b"echo:ls")
        ws.send_text(json.dumps({"t": "r", "c": 120, "r": 40}))
        ws.send_text(json.dumps({"t": "i", "d": "exit\r"}))
        exit_msg = None
        while exit_msg is None:
            msg = ws.receive()
            if msg.get("text"):
                exit_msg = json.loads(msg["text"])
        assert exit_msg == {"t": "exit", "code": 0}
    created = docker_fake.api.created[0]
    assert created["workdir"] == "/data"
    assert created["user"] == "1000:100"
    assert created["tty"] and created["stdin"]
    assert created["environment"]["TERM"] == "xterm-256color"
    assert (100, 30) in docker_fake.api.resized and (120, 40) in docker_fake.api.resized


def test_shell_as_root(client, docker_fake, shell_server):
    with client.websocket_connect(f"/api/servers/{shell_server}/shell?root=true") as ws:
        _read_until(ws, b"$ ")
    assert docker_fake.api.created[0]["user"] == "0:0"


def test_stopped_server_gets_helper_container(client, docker_fake, shell_server):
    server = next(c for c in docker_fake.containers.items.values() if c.labels.get("mcsm.managed"))
    server.status = "exited"
    with client.websocket_connect(f"/api/servers/{shell_server}/shell") as ws:
        note = json.loads(ws.receive_text())
        assert note["t"] == "note" and "helper container" in note["d"]
        _read_until(ws, b"$ ")
        helper = docker_fake.containers.items[f"{server.name}_shell"]
        assert helper.kwargs["volumes_from"] == [server.id]
        assert helper.kwargs["entrypoint"] == ["sleep", "infinity"]
        assert helper.labels == {SHELL_LABEL: shell_server}
        assert docker_fake.api.created[0]["container"] == helper.id
    assert helper.removed
    # The helper never shows up as a discovered server.
    assert not looks_like_minecraft(helper.attrs)


def test_shell_unknown_server(client, docker_fake):
    docker_fake.api = FakeExecApi()
    with client.websocket_connect("/api/servers/nope/shell") as ws:
        msg = json.loads(ws.receive_text())
    assert msg["t"] == "error" and "not found" in msg["d"]


def test_shell_refuses_cross_site_origin(client, shell_server):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/api/servers/{shell_server}/shell", headers={"origin": "https://evil.example"}):
            pass
    assert e.value.code == 4403


@pytest.mark.parametrize(
    "host,headers,ok",
    [
        ("10.0.0.5", {"host": "10.0.0.101:8090", "origin": "http://10.0.0.101:8090"}, True),
        ("192.168.1.20", {"host": "nas:8090"}, True),
        ("::ffff:10.0.0.5", {"host": "x"}, True),
        ("8.8.8.8", {"host": "10.0.0.101:8090"}, False),
        ("10.0.0.5", {"host": "10.0.0.101:8090", "origin": "http://evil.example"}, False),
        ("172.17.0.2", {"host": "mc.example.com", "cf-connecting-ip": "1.2.3.4"}, False),
        ("172.17.0.2", {"host": "x", "x-forwarded-for": "1.2.3.4, 10.0.0.1"}, False),
        ("172.17.0.2", {"host": "x", "x-forwarded-for": "10.0.0.7"}, True),
        (None, {}, False),
    ],
)
def test_request_allowed(host, headers, ok):
    assert request_allowed(host, headers)[0] is ok


# ---- pack modes ------------------------------------------------------------------


def _pack(data: str):
    for mode, value in (("normal", "easy"), ("expert", "hard")):
        d = os.path.join(data, "config-overrides", mode, "gregtech")
        os.makedirs(d)
        with open(os.path.join(d, "gregtech.cfg"), "w") as f:
            f.write(f"difficulty={value}\n")
    os.makedirs(os.path.join(data, "config", "gregtech"))
    with open(os.path.join(data, "config", "gregtech", "gregtech.cfg"), "w") as f:
        f.write("difficulty=easy\n")
    with open(os.path.join(data, "config", "other.cfg"), "w") as f:
        f.write("keep\n")


class FakeBackups:
    def __init__(self):
        self.taken = []

    def take_backup(self, server_id, kind, scope):
        self.taken.append((server_id, kind, scope))
        return type("B", (), {"id": f"{server_id}_backup"})()


def test_pack_mode_detect_and_switch(client, mgr, shell_server, monkeypatch):
    data = mgr.local_data_path(shell_server)
    _pack(data)
    backups = FakeBackups()
    monkeypatch.setattr("app.routers.shell.get_backups", lambda: backups)
    pack_modes._jobs.clear()

    info = client.get(f"/api/servers/{shell_server}/pack-mode").json()
    assert info["supported"] and info["modes"] == ["expert", "normal"] and info["current"] == "normal"

    monkeypatch.setattr(pack_modes.threading, "Thread", _SyncThread)
    r = client.post(f"/api/servers/{shell_server}/pack-mode", json={"mode": "expert"})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["job"]["state"] == "done", info["job"]
    assert info["current"] == "expert"
    assert backups.taken == [(shell_server, "pre-script", "full")]
    with open(os.path.join(data, "config", "gregtech", "gregtech.cfg")) as f:
        assert f.read() == "difficulty=hard\n"
    with open(os.path.join(data, "config", "other.cfg")) as f:
        assert f.read() == "keep\n"
    with open(os.path.join(data, ".mode")) as f:
        assert f.read().strip() == "expert"
    # It was running, so it was stopped for the switch and started again.
    assert client.get(f"/api/servers/{shell_server}").json()["status"] in ("running", "starting")
    assert not mgr.busy(shell_server)


def test_pack_mode_unknown_mode(client, mgr, shell_server):
    _pack(mgr.local_data_path(shell_server))
    r = client.post(f"/api/servers/{shell_server}/pack-mode", json={"mode": "../../etc"})
    assert r.status_code == 400


def test_pack_mode_unsupported_pack(client, shell_server):
    info = client.get(f"/api/servers/{shell_server}/pack-mode").json()
    assert info == {"supported": False, "modes": [], "current": "", "job": info["job"]}


def test_prepare_stops_and_backs_up(client, shell_server, monkeypatch):
    backups = FakeBackups()
    monkeypatch.setattr("app.routers.shell.get_backups", lambda: backups)
    r = client.post(f"/api/servers/{shell_server}/shell/prepare")
    assert r.status_code == 200, r.text
    assert r.json() == {"stopped": True, "backup_id": f"{shell_server}_backup"}
    assert backups.taken == [(shell_server, "pre-script", "full")]
    assert client.get(f"/api/servers/{shell_server}").json()["status"] not in ("running", "starting")


class _SyncThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()
