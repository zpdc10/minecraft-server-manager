"""Finding Minecraft servers the app didn't create, and servers whose container was lost."""
# ruff: noqa: F811  (pytest fixtures imported from test_servers)

import json
import os

import pytest

from app.services.docker_manager import looks_like_minecraft
from tests.test_servers import (  # noqa: F401  (fixtures)
    FakeContainer,
    client,
    docker_fake,
    mgr,
    modrinth_req,
    settings,
)


class HostContainer(FakeContainer):
    """A container someone made outside the app, with full `docker inspect` attrs."""

    def __init__(self, name, image, env=None, mounts=None, ports=None, memory=0, status="running"):
        super().__init__(name, {"org.opencontainers.image.title": "x"})
        self.image = image
        self.env = env or {}
        self.mounts = mounts or []
        self.ports = ports or {}
        self.memory = memory
        self.status = status
        self.updates = []

    @property
    def attrs(self):
        a = super().attrs
        a["Config"] = {
            "Image": self.image,
            "Env": [f"{k}={v}" for k, v in self.env.items()],
            "ExposedPorts": {p: {} for p in self.ports},
        }
        a["HostConfig"] = {
            "PortBindings": {p: [{"HostIp": "", "HostPort": str(h)}] for p, h in self.ports.items()},
            "Memory": self.memory,
        }
        a["Mounts"] = self.mounts
        a["Created"] = "2026-10-01T12:00:00.123456789Z"
        return a

    def update(self, **kwargs):
        self.updates.append(kwargs)


class SelfContainer(FakeContainer):
    def __init__(self, mounts):
        super().__init__("minecraft-server-manager", {})
        self.mounts = mounts

    @property
    def attrs(self):
        return {
            "State": {"Status": "running"},
            "Config": {"Image": "ghcr.io/zpdc10/minecraft-server-manager:latest", "Env": []},
            "Mounts": self.mounts,
        }


def add(docker_fake, c):
    docker_fake.containers.items[c.name] = c
    return c


@pytest.fixture
def appdata(tmp_path, docker_fake, monkeypatch):
    """The manager container has /mnt/user/appdata mounted at <tmp>/appdata."""
    local = tmp_path / "appdata"
    local.mkdir()
    monkeypatch.setattr("socket.gethostname", lambda: "minecraft-server-manager")
    add(docker_fake, SelfContainer([{"Source": "/mnt/user/appdata", "Destination": str(local)}]))
    return local


def survival(docker_fake, appdata, **kw):
    (appdata / "survival").mkdir(exist_ok=True)
    (appdata / "survival" / "server.properties").write_text("motd=Hi\nlevel-name=world\n")
    args = dict(
        name="Survival_World",
        image="itzg/minecraft-server:java17",
        env={"EULA": "TRUE", "TYPE": "PAPER", "VERSION": "1.20.4", "MEMORY": "6G"},
        mounts=[{"Type": "bind", "Source": "/mnt/user/appdata/survival", "Destination": "/data"}],
        ports={"25565/tcp": 25570},
    )
    args.update(kw)
    return add(docker_fake, HostContainer(**args))


# ---- detection -------------------------------------------------------------------


def attrs(image="", env=(), ports=()):
    return {"Config": {"Image": image, "Env": list(env), "ExposedPorts": {p: {} for p in ports}}}


@pytest.mark.parametrize(
    "a,expected",
    [
        (attrs("itzg/minecraft-server:java21"), True),
        (attrs("itzg/minecraft-bedrock-server"), True),
        (attrs("marctv/minecraft-papermc-server:latest"), True),
        (attrs("ghcr.io/someone/purpur:1.21"), True),
        (attrs("custom/thing", env=["EULA=true"]), True),
        (attrs("custom/thing", ports=["25565/tcp"]), True),
        (attrs("jellyfin/jellyfin:latest"), False),
        (attrs("codeberg.org/forgejo/forgejo:9"), False),
        (attrs("itzg/mc-backup"), False),
        (attrs("itzg/mc-router"), False),
        (attrs("ghcr.io/zpdc10/minecraft-server-manager:latest"), False),
        (attrs("jacobsmile/tmodloader1.4:v2025"), False),
    ],
)
def test_looks_like_minecraft(a, expected):
    assert looks_like_minecraft(a) is expected


# ---- servers the app didn't create ---------------------------------------------------


def test_external_server_is_listed(client, docker_fake, appdata):
    survival(docker_fake, appdata)
    add(docker_fake, HostContainer("Jellyfin", "jellyfin/jellyfin:latest", ports={"8096/tcp": 8096}))
    servers = client.get("/api/servers").json()
    assert [s["id"] for s in servers] == ["ext-survival-world"]
    s = servers[0]
    assert s["managed"] is False
    assert s["name"] == "Survival_World"
    assert s["source"] == "external"
    assert s["port"] == 25570
    assert s["memory_gb"] == 6
    assert s["java_version"] == "java17"
    assert s["minecraft_version"] == "1.20.4"
    assert s["loader"] == "paper"
    assert s["status"] == "running"
    assert s["data_path"] == "/mnt/user/appdata/survival"
    assert s["files_available"] is True
    assert s["image"] == "itzg/minecraft-server:java17"


def test_external_start_stop_does_not_change_container(client, docker_fake, appdata):
    c = survival(docker_fake, appdata, status="exited")
    assert client.post("/api/servers/ext-survival-world/start").json()["status"] == "starting"
    assert c.updates == []  # restart policy left alone
    assert client.post("/api/servers/ext-survival-world/stop").json()["status"] == "stopped"
    assert client.get("/api/servers/ext-survival-world/logs").status_code == 200


def test_external_cannot_be_deleted_or_reconfigured(client, docker_fake, appdata):
    c = survival(docker_fake, appdata)
    r = client.delete("/api/servers/ext-survival-world?delete_data=true")
    assert r.status_code == 409
    assert not c.removed
    assert (appdata / "survival").is_dir()
    r = client.patch("/api/servers/ext-survival-world/settings", json={"memory_gb": 8})
    assert r.status_code == 409
    assert client.post("/api/servers/ext-survival-world/recreate").status_code == 409


def test_external_files_are_used(client, docker_fake, appdata):
    survival(docker_fake, appdata)
    r = client.get("/api/servers/ext-survival-world/properties")
    assert r.status_code == 200
    assert r.json()["properties"]["motd"] == "Hi"


def test_external_files_out_of_reach(client, docker_fake, appdata):
    survival(
        docker_fake,
        appdata,
        mounts=[{"Type": "bind", "Source": "/mnt/disk1/minecraft", "Destination": "/data"}],
    )
    s = client.get("/api/servers/ext-survival-world").json()
    assert s["files_available"] is False
    assert "/mnt/disk1/minecraft" in s["note"]
    r = client.get("/api/servers/ext-survival-world/properties")
    assert r.status_code == 409


def test_new_server_cannot_take_external_port(client, docker_fake, appdata):
    survival(docker_fake, appdata)
    r = client.post("/api/servers", json=modrinth_req(port=25570))
    assert r.status_code == 409
    assert "Survival_World" in r.json()["detail"]


def test_external_name_cannot_clash_with_ours(client, docker_fake, appdata):
    survival(docker_fake, appdata)
    r = client.post("/api/servers", json=modrinth_req(name="ext survival world", port=25566))
    assert r.json()["id"] == "server-ext-survival-world"


# ---- our servers whose container was lost -----------------------------------------------


def test_record_saved_on_create(client, tmp_path):
    client.post("/api/servers", json=modrinth_req())
    record = json.loads((tmp_path / "my-pack" / ".mcsm" / "server.json").read_text())
    assert record["info"]["name"] == "My Pack"
    assert record["info"]["port"] == 25565
    assert record["env"]["MODRINTH_MODPACK"] == "cool-pack"
    assert "UID" not in record["env"]


def test_lost_container_is_listed_and_recreated(client, docker_fake, mgr):
    client.post("/api/servers", json=modrinth_req())
    old = docker_fake.containers.items["mcsm-my-pack"]
    old.removed = True  # e.g. Unraid's docker image was rebuilt

    s = client.get("/api/servers").json()
    assert [x["id"] for x in s] == ["my-pack"]
    assert s[0]["status"] == "missing"
    assert s[0]["name"] == "My Pack"
    assert client.post("/api/servers/my-pack/start").status_code == 409

    original = mgr.recreate_server
    mgr.recreate_server = lambda sid, background=True: original(sid, background=False)
    r = client.post("/api/servers/my-pack/recreate")
    assert r.status_code == 202, r.text
    new = docker_fake.containers.items["mcsm-my-pack"]
    assert new is not old
    assert new.labels["mcsm.name"] == "My Pack"
    assert new.kwargs["environment"]["MODRINTH_MODPACK"] == "cool-pack"
    assert new.kwargs["ports"] == {"25565/tcp": 25565}
    assert client.get("/api/servers/my-pack").json()["status"] == "starting"


def test_deleting_keeps_data_but_forgets_server(client, tmp_path):
    client.post("/api/servers", json=modrinth_req())
    assert client.delete("/api/servers/my-pack").status_code == 204
    assert (tmp_path / "my-pack").is_dir()
    assert client.get("/api/servers").json() == []


def test_forget_missing_server(client, docker_fake, tmp_path):
    client.post("/api/servers", json=modrinth_req())
    docker_fake.containers.items["mcsm-my-pack"].removed = True
    assert client.delete("/api/servers/my-pack").status_code == 204
    assert client.get("/api/servers").json() == []
    assert (tmp_path / "my-pack").is_dir()


def test_older_servers_get_a_record(client, tmp_path):
    client.post("/api/servers", json=modrinth_req())
    record = tmp_path / "my-pack" / ".mcsm" / "server.json"
    os.remove(record)
    client.get("/api/servers")
    assert json.loads(record.read_text())["info"]["id"] == "my-pack"


def test_our_container_without_labels_stays_ours(client, docker_fake):
    client.post("/api/servers", json=modrinth_req())
    c = docker_fake.containers.items["mcsm-my-pack"]
    c.labels = {}
    c.kwargs["environment"] = {"EULA": "TRUE"}
    s = client.get("/api/servers").json()
    assert [(x["id"], x["managed"], x["name"]) for x in s] == [("my-pack", True, "My Pack")]
