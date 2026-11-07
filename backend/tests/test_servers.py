"""Server management tests, using an in-memory fake of the Docker SDK."""

import dataclasses

import pytest
from docker.errors import ImageNotFound, NotFound
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.models.server import CreateServerRequest
from app.routers.servers import get_manager
from app.services.docker_manager import DockerManager
from app.services.itzg_env import EnvBuildError, build_env, pick_java_version


class FakeContainer:
    def __init__(self, name, labels, **kwargs):
        self.name = name
        self.labels = labels
        self.kwargs = kwargs
        self.status = "created"
        self.exit_code = 0
        self.health = None
        self.removed = False

    @property
    def attrs(self):
        state = {"Status": self.status, "ExitCode": self.exit_code}
        if self.health:
            state["Health"] = {"Status": self.health}
        return {"State": state}

    def start(self):
        self.status, self.health = "running", "starting"

    def stop(self, timeout=10):
        self.status, self.exit_code = "exited", 0

    def restart(self, timeout=10):
        self.start()

    def reload(self):
        pass

    def update(self, **kwargs):
        self.kwargs.update(kwargs)

    def remove(self, force=False):
        self.removed = True

    def logs(self, tail=200):
        return b"[init] Starting the Minecraft server...\n"


class FakeContainers:
    def __init__(self):
        self.items: dict[str, FakeContainer] = {}

    def list(self, all=False, filters=None):
        items = [c for c in self.items.values() if not c.removed]
        label = (filters or {}).get("label")
        if label:
            key, _, value = label.partition("=")
            items = [c for c in items if (c.labels or {}).get(key) == value]
        return items

    def get(self, name):
        c = self.items.get(name)
        if c is None or c.removed:
            raise NotFound("no such container")
        return c

    def create(self, image, name, labels, **kwargs):
        c = FakeContainer(name, labels, image=image, **kwargs)
        self.items[name] = c
        return c


class FakeImages:
    def __init__(self):
        self.pulled = []

    def get(self, name):
        if name not in self.pulled:
            raise ImageNotFound("missing")

    def pull(self, repo, tag):
        self.pulled.append(f"{repo}:{tag}")


class FakeDocker:
    def __init__(self):
        self.containers = FakeContainers()
        self.images = FakeImages()


@pytest.fixture
def settings(tmp_path):
    return dataclasses.replace(Settings(), data_root=str(tmp_path), host_data_root="/mnt/user/appdata/mc-servers")


@pytest.fixture
def docker_fake():
    return FakeDocker()


@pytest.fixture
def mgr(settings, docker_fake):
    return DockerManager(settings, client=docker_fake)


@pytest.fixture
def client(mgr):
    app.dependency_overrides[get_manager] = lambda: mgr
    # Provision synchronously so tests don't race the background thread.
    original = mgr.create_server
    mgr.create_server = lambda req, background=True: original(req, background=False)
    yield TestClient(app)
    app.dependency_overrides.clear()


def modrinth_req(**kw):
    base = dict(
        name="My Pack",
        source="modrinth",
        project_id="abc123",
        project_slug="cool-pack",
        version_id="v1",
        minecraft_version="1.20.1",
        port=25565,
        memory_gb=6,
        # The install-and-test builder has its own tests in test_builder.py.
        auto_fix=False,
    )
    return {**base, **kw}


# ---- env building -----------------------------------------------------------


@pytest.mark.parametrize(
    "mc,java",
    [
        ("1.12.2", "java8"),
        ("1.16.5", "java8"),
        ("1.17.1", "java16"),
        ("1.18.2", "java17"),
        ("1.20.4", "java17"),
        ("1.20.5", "java21"),
        ("1.21.1", "java21"),
        ("", "latest"),
        ("24w14a", "latest"),
    ],
)
def test_pick_java_version(mc, java):
    assert pick_java_version(mc) == java


def test_modrinth_env(settings):
    env = build_env(CreateServerRequest(**modrinth_req()), settings)
    assert env["EULA"] == "TRUE"
    assert env["TYPE"] == "MODRINTH"
    assert env["MODRINTH_MODPACK"] == "cool-pack"
    assert env["MODRINTH_VERSION"] == "v1"
    assert env["MEMORY"] == "6G"


def test_curseforge_needs_api_key(settings, monkeypatch):
    monkeypatch.setattr("app.settings_store.curseforge_api_key", lambda: "")
    req = CreateServerRequest(name="cf", source="curseforge", project_slug="all-the-mods-9", version_id="123")
    with pytest.raises(EnvBuildError, match="API key"):
        build_env(req, settings)
    monkeypatch.setattr("app.settings_store.curseforge_api_key", lambda: "k$y")
    env = build_env(req, settings)
    assert env["TYPE"] == "AUTO_CURSEFORGE"
    assert env["CF_API_KEY"] == "k$y"
    assert env["CF_SLUG"] == "all-the-mods-9"
    assert env["CF_FILE_ID"] == "123"


def test_extra_env_cannot_disable_eula(settings):
    env = build_env(CreateServerRequest(**modrinth_req(extra_env={"EULA": "FALSE", "DIFFICULTY": "hard"})), settings)
    assert env["EULA"] == "TRUE"
    assert env["DIFFICULTY"] == "hard"


# ---- API --------------------------------------------------------------------


def test_create_and_list(client, docker_fake, tmp_path):
    r = client.post("/api/servers", json=modrinth_req())
    assert r.status_code == 202, r.text
    assert r.json()["id"] == "my-pack"

    assert docker_fake.images.pulled == ["itzg/minecraft-server:java17"]
    c = docker_fake.containers.items["mcsm-my-pack"]
    assert c.kwargs["ports"] == {"25565/tcp": 25565}
    assert c.kwargs["volumes"] == {"/mnt/user/appdata/mc-servers/my-pack": {"bind": "/data", "mode": "rw"}}
    assert c.kwargs["environment"]["UID"] == "99"
    assert c.kwargs["mem_limit"] == f"{6 * 1024 + 1536}m"
    assert (tmp_path / "my-pack").is_dir()

    servers = client.get("/api/servers").json()
    assert len(servers) == 1
    s = servers[0]
    assert (s["name"], s["port"], s["memory_gb"], s["status"]) == ("My Pack", 25565, 6, "starting")

    c.health = "healthy"
    assert client.get("/api/servers/my-pack").json()["status"] == "running"


def test_duplicate_port_rejected(client):
    assert client.post("/api/servers", json=modrinth_req()).status_code == 202
    r = client.post("/api/servers", json=modrinth_req(name="Other"))
    assert r.status_code == 409
    assert "25565" in r.json()["detail"]


def test_same_name_gets_unique_id(client):
    client.post("/api/servers", json=modrinth_req())
    r = client.post("/api/servers", json=modrinth_req(port=25566))
    assert r.json()["id"] == "my-pack-2"


def test_curseforge_without_key_is_400(client, monkeypatch):
    monkeypatch.setattr("app.settings_store.curseforge_api_key", lambda: "")
    r = client.post("/api/servers", json={"name": "cf", "source": "curseforge", "project_slug": "atm9"})
    assert r.status_code == 400


def test_invalid_port_and_memory(client):
    assert client.post("/api/servers", json=modrinth_req(port=80)).status_code == 422
    assert client.post("/api/servers", json=modrinth_req(memory_gb=0)).status_code == 422


def test_start_stop_restart(client, docker_fake):
    client.post("/api/servers", json=modrinth_req())
    assert client.post("/api/servers/my-pack/stop").json()["status"] == "stopped"
    assert client.post("/api/servers/my-pack/start").json()["status"] == "starting"
    assert client.post("/api/servers/my-pack/restart").status_code == 200


def test_crash_shows_error(client, docker_fake):
    client.post("/api/servers", json=modrinth_req())
    c = docker_fake.containers.items["mcsm-my-pack"]
    c.status, c.exit_code = "exited", 1
    s = client.get("/api/servers/my-pack").json()
    assert s["status"] == "error"
    assert "code 1" in s["error"]


def test_delete_keeps_data_by_default(client, tmp_path):
    client.post("/api/servers", json=modrinth_req())
    assert client.delete("/api/servers/my-pack").status_code == 204
    assert (tmp_path / "my-pack").is_dir()
    assert client.get("/api/servers").json() == []


def test_delete_with_data(client, tmp_path):
    client.post("/api/servers", json=modrinth_req())
    (tmp_path / "my-pack" / "world").mkdir()
    assert client.delete("/api/servers/my-pack?delete_data=true").status_code == 204
    assert not (tmp_path / "my-pack").exists()


def test_unknown_server_404(client):
    assert client.get("/api/servers/nope").status_code == 404
    assert client.post("/api/servers/nope/start").status_code == 404
    assert client.delete("/api/servers/nope").status_code == 404


def test_logs(client):
    client.post("/api/servers", json=modrinth_req())
    r = client.get("/api/servers/my-pack/logs?tail=50")
    assert r.status_code == 200
    assert "Starting the Minecraft server" in r.text


def test_pull_failure_reported(client, docker_fake):
    from docker.errors import APIError

    def boom(repo, tag):
        raise APIError("pull failed", explanation="network unreachable")

    docker_fake.images.pull = boom
    r = client.post("/api/servers", json=modrinth_req())
    assert r.status_code == 202
    s = client.get("/api/servers/my-pack").json()
    assert s["status"] == "error"
    assert "network unreachable" in s["error"]
    # A failed server can be removed from the list.
    assert client.delete("/api/servers/my-pack").status_code == 204


class SelfContainer(FakeContainer):
    @property
    def attrs(self):
        return {
            "State": {"Status": "running"},
            "Mounts": [
                {"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"},
                {"Source": "/mnt/user/appdata/minecraft-server-manager", "Destination": "/data"},
            ],
        }


def test_host_data_root_detected_from_own_mounts(docker_fake, monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "selfid")
    docker_fake.containers.items["selfid"] = SelfContainer("selfid", {})
    s = dataclasses.replace(Settings(), data_root="/data/servers", host_data_root="")
    m = DockerManager(s, client=docker_fake)
    assert m.host_data_root == "/mnt/user/appdata/minecraft-server-manager/servers"


def test_host_data_root_falls_back_outside_container(docker_fake):
    s = dataclasses.replace(Settings(), data_root="/srv/mc", host_data_root="")
    assert DockerManager(s, client=docker_fake).host_data_root == "/srv/mc"
