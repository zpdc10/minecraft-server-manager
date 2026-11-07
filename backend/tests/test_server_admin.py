"""Tests for the server management tools (console, players, properties, settings, mods)."""
# ruff: noqa: F811  (pytest fixtures imported from test_servers)

import pytest

from app.services.server_admin import parse_player_list, parse_properties, render_properties
from tests.test_servers import (  # noqa: F401  (fixtures)
    FakeContainer,
    client,
    docker_fake,
    mgr,
    modrinth_req,
    settings,
)


class AdminContainer(FakeContainer):
    """FakeContainer plus env, docker exec and stats."""

    replies: dict[str, tuple[int, bytes]] = {}

    def __init__(self, name, labels, **kwargs):
        super().__init__(name, labels, **kwargs)
        self.commands: list[str] = []

    @property
    def attrs(self):
        a = super().attrs
        env = [f"{k}={v}" for k, v in self.kwargs.get("environment", {}).items()]
        a["Config"] = {"Env": ["PATH=/usr/bin", *env], "Image": self.kwargs.get("image", "")}
        a["State"]["StartedAt"] = "2026-11-07T00:00:00Z"
        return a

    def exec_run(self, cmd):
        assert cmd[0] == "rcon-cli"
        self.commands.append(cmd[1])
        for prefix, reply in self.replies.items():
            if cmd[1].startswith(prefix):
                return reply
        return 0, f"ran {cmd[1]}".encode()

    def stats(self, stream=False):
        return {
            "cpu_stats": {"cpu_usage": {"total_usage": 300}, "system_cpu_usage": 2000, "online_cpus": 4},
            "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
            "memory_stats": {"usage": 3 * 2**30, "limit": 8 * 2**30, "stats": {"inactive_file": 2**30}},
        }


class FakeImage:
    attrs = {"Config": {"Env": ["PATH=/usr/bin"]}}


@pytest.fixture
def server(client, docker_fake, tmp_path):
    def create(image, name, labels, **kwargs):
        c = AdminContainer(name, labels, image=image, **kwargs)
        docker_fake.containers.items[name] = c
        return c

    docker_fake.containers.create = create
    docker_fake.images.get = lambda name: FakeImage()
    AdminContainer.replies = {"list": (0, b"There are 1 of a max of 20 players online: \xc2\xa7aSteve")}
    r = client.post("/api/servers", json=modrinth_req())
    assert r.status_code == 202, r.text
    c = docker_fake.containers.items["mcsm-my-pack"]
    c.health = "healthy"
    (tmp_path / "my-pack" / "server.properties").write_text(
        "#Minecraft server properties\nmotd=My Pack\ndifficulty=easy\nwhite-list=false\nserver-port=25565\n"
    )
    return c


def test_parse_player_list():
    assert parse_player_list("There are 2 of a max of 20 players online: Alex, Steve") == (2, 20, ["Alex", "Steve"])
    assert parse_player_list("There are 0 of a max of 10 players online:") == (0, 10, [])
    assert parse_player_list("There are 1/8 players online:\ndefault: Bob") == (1, 8, ["Bob"])


def test_properties_round_trip():
    text = "#c\nmotd=Hi\nmax-players=20\n"
    assert parse_properties(text) == {"motd": "Hi", "max-players": "20"}
    assert render_properties(text, {"max-players": "5", "pvp": "false"}) == "#c\nmotd=Hi\nmax-players=5\npvp=false\n"


def test_console(client, server):
    r = client.post("/api/servers/my-pack/console", json={"command": "/say hi"})
    assert r.status_code == 200, r.text
    assert r.json() == {"command": "say hi", "output": "ran say hi"}
    assert server.commands[-1] == "say hi"


def test_console_needs_running_server(client, server):
    client.post("/api/servers/my-pack/stop")
    r = client.post("/api/servers/my-pack/console", json={"command": "list"})
    assert r.status_code == 409


def test_console_rejects_multiline(client, server):
    r = client.post("/api/servers/my-pack/console", json={"command": "say a\nop Bob"})
    assert r.status_code == 400


def test_players(client, server, tmp_path):
    (tmp_path / "my-pack" / "ops.json").write_text('[{"uuid": "u1", "name": "Alex", "level": 4}]')
    (tmp_path / "my-pack" / "whitelist.json").write_text('[{"uuid": "u2", "name": "Steve"}]')
    p = client.get("/api/servers/my-pack/players").json()
    assert p["online"] == ["Steve"] and p["online_count"] == 1 and p["max_players"] == 20
    assert p["console_ready"] is True
    assert p["ops"][0]["name"] == "Alex" and p["ops"][0]["level"] == 4
    assert p["whitelist"][0]["name"] == "Steve"
    assert p["whitelist_enabled"] is False


def test_player_actions(client, server):
    r = client.post("/api/servers/my-pack/players", json={"action": "kick", "name": "Steve", "reason": "be  nice"})
    assert r.status_code == 200, r.text
    assert server.commands[-1] == "kick Steve be nice"
    client.post("/api/servers/my-pack/players", json={"action": "whitelist_add", "name": "Alex"})
    assert server.commands[-1] == "whitelist add Alex"
    bad = client.post("/api/servers/my-pack/players", json={"action": "op", "name": "Bob; stop"})
    assert bad.status_code == 400
    bad = client.post("/api/servers/my-pack/players", json={"action": "stop", "name": "Bob"})
    assert bad.status_code == 400


def test_whitelist_toggle_when_stopped_edits_file(client, server, tmp_path):
    client.post("/api/servers/my-pack/stop")
    r = client.put("/api/servers/my-pack/whitelist", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert "white-list=true" in (tmp_path / "my-pack" / "server.properties").read_text()


def test_properties_edit_and_locked(client, server, tmp_path):
    info = client.get("/api/servers/my-pack/properties").json()
    assert info["properties"]["difficulty"] == "easy"
    assert "motd" in info["locked"] and "server-port" in info["locked"]
    r = client.put("/api/servers/my-pack/properties", json={"properties": {"difficulty": "hard", "motd": "My Pack"}})
    assert r.status_code == 200, r.text
    text = (tmp_path / "my-pack" / "server.properties").read_text()
    assert "difficulty=hard" in text and text.startswith("#Minecraft")
    r = client.put("/api/servers/my-pack/properties", json={"properties": {"server-port": "1"}})
    assert r.status_code == 400
    r = client.put("/api/servers/my-pack/properties", json={"properties": {"bad key": "1"}})
    assert r.status_code == 400


def test_update_settings_recreates_container(client, server, docker_fake):
    r = client.patch("/api/servers/my-pack/settings", json={"memory_gb": 8, "port": 25570, "motd": "Hello"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["memory_gb"] == 8 and body["port"] == 25570
    assert server.removed
    new = docker_fake.containers.items["mcsm-my-pack"]
    assert new is not server
    env = new.kwargs["environment"]
    assert env["MEMORY"] == "8G" and env["MOTD"] == "Hello" and env["MODRINTH_MODPACK"] == "cool-pack"
    assert "PATH" not in env
    assert new.kwargs["ports"] == {"25565/tcp": 25570}
    assert new.status == "running"  # it was running, so it was started again


def test_update_settings_keeps_stopped_server_stopped(client, server, docker_fake):
    client.post("/api/servers/my-pack/stop")
    r = client.patch("/api/servers/my-pack/settings", json={"memory_gb": 2})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "stopped"


def test_update_settings_port_conflict(client, server):
    client.post("/api/servers", json=modrinth_req(name="Other", port=25600))
    r = client.patch("/api/servers/my-pack/settings", json={"port": 25600})
    assert r.status_code == 409


def test_stats(client, server):
    s = client.get("/api/servers/my-pack/stats").json()
    assert s["running"] is True
    assert s["cpu_percent"] == 80.0
    assert s["memory_used_mb"] == 2048 and s["memory_limit_mb"] == 8192


def test_mods(client, server, tmp_path):
    mods = tmp_path / "my-pack" / "mods"
    mods.mkdir()
    (mods / "a.jar").write_bytes(b"PK..")
    r = client.post("/api/servers/my-pack/mods?filename=b.jar", content=b"PK\x03\x04data")
    assert r.status_code == 200, r.text
    assert [m["file"] for m in r.json()] == ["a.jar", "b.jar"]
    assert client.post("/api/servers/my-pack/mods?filename=c.jar", content=b"nope").status_code == 400
    assert client.post("/api/servers/my-pack/mods?filename=..%2Fx.jar", content=b"PK").status_code == 400

    r = client.post("/api/servers/my-pack/mods/a.jar/disable").json()
    assert r[0] == {**r[0], "file": "a.jar.disabled", "enabled": False}
    client.post("/api/servers/my-pack/mods/a.jar.disabled/enable")
    assert (mods / "a.jar").exists()

    r = client.delete("/api/servers/my-pack/mods/a.jar")
    assert [m["file"] for m in r.json()] == ["b.jar"]
    assert (tmp_path / "my-pack" / ".mcsm" / "removed-mods" / "a.jar").exists()
