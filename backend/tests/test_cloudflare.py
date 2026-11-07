"""Cloudflare tunnel and DNS automation, against an in-memory fake of the Cloudflare API."""

import dataclasses
import itertools
import json
import re

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.routers.servers import get_manager
from app.services import cloudflare as cf_mod
from app.services import playit
from app.services.docker_manager import DockerManager

from .test_servers import FakeDocker, modrinth_req

API = "https://api.cloudflare.com/client/v4"
TOKEN = "cf-token-1234567890"
ZONE = {"id": "zone1", "name": "example.com", "account": {"id": "acct1", "name": "Zac"}}


class FakeCloudflare:
    """Just enough of the Cloudflare API: zones, tunnels, tunnel config, DNS records."""

    def __init__(self):
        self.records: dict[str, dict] = {}
        self.tunnels: dict[str, dict] = {}
        self.configs: dict[str, dict] = {}
        self.ip = "203.0.113.7"
        self._ids = itertools.count(1)

    def ok(self, result, status=200):
        return httpx.Response(status, json={"success": True, "errors": [], "result": result})

    def fail(self, status, message):
        return httpx.Response(status, json={"success": False, "errors": [{"message": message}], "result": None})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "1.1.1.1":
            return httpx.Response(200, text=f"fl=1\nip={self.ip}\nts=1\n")
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return self.fail(403, "Authentication error")
        path = request.url.path.removeprefix("/client/v4")
        m, body = request.method, json.loads(request.content or b"null")
        if path == "/zones":
            return self.ok([ZONE])
        if path == "/accounts/acct1/cfd_tunnel":
            if m == "POST":
                tid = f"tun{next(self._ids)}"
                self.tunnels[tid] = {"id": tid, "name": body["name"], "status": "inactive", "connections": []}
                return self.ok(self.tunnels[tid])
            name = request.url.params.get("name")
            return self.ok([t for t in self.tunnels.values() if t["name"] == name])
        if mt := re.fullmatch(r"/accounts/acct1/cfd_tunnel/([^/]+)(/.*)?", path):
            tid, rest = mt.group(1), mt.group(2) or ""
            if tid not in self.tunnels:
                return self.fail(404, "Tunnel not found")
            if rest == "/token":
                return self.ok(f"token-for-{tid}")
            if rest == "/configurations":
                self.configs[tid] = body["config"]
                return self.ok({})
            if rest == "/connections":
                return self.ok(None)
            if m == "DELETE":
                del self.tunnels[tid]
                return self.ok({})
            return self.ok(self.tunnels[tid])
        if path == "/zones/zone1/dns_records":
            if m == "POST":
                rid = f"rec{next(self._ids)}"
                self.records[rid] = {**body, "id": rid}
                return self.ok(self.records[rid])
            name = request.url.params.get("name")
            return self.ok([r for r in self.records.values() if r["name"] == name])
        if mr := re.fullmatch(r"/zones/zone1/dns_records/([^/]+)", path):
            rid = mr.group(1)
            if rid not in self.records:
                return self.fail(404, "Record not found")
            if m == "DELETE":
                del self.records[rid]
                return self.ok({"id": rid})
            if m == "PATCH":
                self.records[rid].update(body)
            return self.ok(self.records[rid])
        return self.fail(404, f"unexpected {m} {path}")

    def names(self):
        return sorted((r["type"], r["name"]) for r in self.records.values())


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = dataclasses.replace(Settings(), data_dir=str(tmp_path / "app"), data_root=str(tmp_path / "servers"))
    monkeypatch.setattr("app.settings_store.settings", s)
    monkeypatch.setattr(cf_mod, "settings", s)
    fake = FakeCloudflare()
    monkeypatch.setattr(cf_mod, "_instance", cf_mod.Cloudflare(transport=httpx.MockTransport(fake)))
    docker = FakeDocker()
    mgr = DockerManager(s, client=docker)
    app.dependency_overrides[get_manager] = lambda: mgr
    original = mgr.create_server
    mgr.create_server = lambda req, background=True: original(req, background=False)
    yield fake, TestClient(app), docker
    app.dependency_overrides.clear()


def connect(client, mode="tunnel", auto=True):
    r = client.put("/api/cloudflare/token", json={"api_token": TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()[0]["name"] == "example.com"
    r = client.put("/api/cloudflare/config", json={"zone_id": "zone1", "auto_route": auto, "default_mode": mode})
    assert r.status_code == 200, r.text
    return r.json()


def test_bad_token_is_not_saved(env):
    _, client, _ = env
    r = client.put("/api/cloudflare/token", json={"api_token": "nope"})
    assert r.status_code == 400
    assert "permissions" in r.json()["detail"]
    assert client.get("/api/cloudflare").json()["token_set"] is False


def test_connect_makes_own_tunnel_and_connector(env):
    fake, client, docker = env
    status = connect(client)
    assert status["zone_name"] == "example.com"
    assert status["connector_status"] == "running"
    (tunnel,) = fake.tunnels.values()
    assert tunnel["name"] == "minecraft-server-manager"
    c = docker.containers.get("mcsm_cloudflared")
    assert c.kwargs["environment"] == {"TUNNEL_TOKEN": f"token-for-{tunnel['id']}"}
    assert c.kwargs["network_mode"] == "host"
    # The token is never sent back to the browser.
    assert TOKEN not in json.dumps(client.get("/api/cloudflare").json())
    # Connecting again reuses the same tunnel instead of making another.
    connect(client)
    assert len(fake.tunnels) == 1


def test_new_server_gets_tunnel_address_and_delete_cleans_up(env):
    fake, client, _ = env
    connect(client)
    r = client.post("/api/servers", json=modrinth_req(name="Survival", port=25570))
    assert r.status_code == 202
    (route,) = client.get("/api/cloudflare/routes").json()
    assert route["hostname"] == "survival.example.com"
    assert route["mode"] == "tunnel"
    assert "cloudflared access tcp --hostname survival.example.com" in route["player_command"]
    (rec,) = fake.records.values()
    tid = next(iter(fake.tunnels))
    assert rec["type"] == "CNAME" and rec["proxied"] is True and rec["content"] == f"{tid}.cfargotunnel.com"
    assert fake.configs[tid]["ingress"][0] == {
        "hostname": "survival.example.com",
        "service": "tcp://localhost:25570",
    }

    assert client.delete("/api/servers/survival").status_code == 204
    assert fake.records == {}
    assert fake.configs[tid]["ingress"] == [{"service": "http_status:404"}]
    assert client.get("/api/cloudflare/routes").json() == []


def test_auto_route_off_leaves_new_servers_alone(env):
    fake, client, _ = env
    connect(client, auto=False)
    client.post("/api/servers", json=modrinth_req(name="Quiet"))
    assert client.get("/api/cloudflare/routes").json() == []
    assert fake.records == {}


def test_direct_mode_makes_a_and_srv_records(env):
    fake, client, _ = env
    connect(client, mode="direct")
    client.post("/api/servers", json=modrinth_req(name="Creative", port=25580))
    assert fake.names() == [("A", "creative.example.com"), ("SRV", "_minecraft._tcp.creative.example.com")]
    a = next(r for r in fake.records.values() if r["type"] == "A")
    srv = next(r for r in fake.records.values() if r["type"] == "SRV")
    assert a["content"] == "203.0.113.7" and a["proxied"] is False
    assert srv["data"]["port"] == 25580
    (route,) = client.get("/api/cloudflare/routes").json()
    assert route["connect_address"] == "creative.example.com"

    # A new public IP is picked up on the next sync.
    fake.ip = "198.51.100.9"
    client.post("/api/cloudflare/sync")
    assert next(r for r in fake.records.values() if r["type"] == "A")["content"] == "198.51.100.9"


def test_never_overwrites_records_it_did_not_make(env):
    fake, client, _ = env
    connect(client, auto=False)
    client.post("/api/servers", json=modrinth_req(name="Lobby"))
    fake.records["theirs"] = {"id": "theirs", "type": "A", "name": "lobby.example.com", "content": "1.2.3.4"}
    r = client.put("/api/cloudflare/routes/lobby", json={})
    assert r.status_code == 409
    assert fake.records["theirs"]["content"] == "1.2.3.4"
    # A different name works.
    r = client.put("/api/cloudflare/routes/lobby", json={"subdomain": "hub"})
    assert r.status_code == 200, r.text
    assert r.json()["hostname"] == "hub.example.com"


def test_rename_and_switch_mode_replaces_records(env):
    fake, client, _ = env
    connect(client, auto=False)
    client.post("/api/servers", json=modrinth_req(name="Main"))
    client.put("/api/cloudflare/routes/main", json={"subdomain": "play"})
    client.put("/api/cloudflare/routes/main", json={"subdomain": "play", "mode": "direct"})
    assert fake.names() == [("A", "play.example.com"), ("SRV", "_minecraft._tcp.play.example.com")]
    tid = next(iter(fake.tunnels))
    assert fake.configs[tid]["ingress"] == [{"service": "http_status:404"}]


def test_rejects_multi_level_names(env):
    _, client, _ = env
    connect(client, auto=False)
    client.post("/api/servers", json=modrinth_req(name="Main"))
    r = client.put("/api/cloudflare/routes/main", json={"subdomain": "mc.play"})
    assert r.status_code == 400


def test_disconnect_removes_everything_it_made(env):
    fake, client, docker = env
    connect(client)
    client.post("/api/servers", json=modrinth_req(name="One"))
    fake.records["theirs"] = {"id": "theirs", "type": "A", "name": "www.example.com", "content": "1.2.3.4"}
    assert client.delete("/api/cloudflare").status_code == 204
    assert list(fake.records) == ["theirs"]
    assert fake.tunnels == {}
    assert "mcsm_cloudflared" not in {c.name for c in docker.containers.list(all=True)}
    assert client.get("/api/cloudflare").json()["token_set"] is False


# ---- playit.gg -----------------------------------------------------------------

DOH = "https://cloudflare-dns.com/dns-query"


def doh_srv(target: str, port: int):
    def handler(request):
        if request.url.params.get("type") == "SRV":
            return httpx.Response(200, json={"Answer": [{"type": 33, "data": f"0 0 {port} {target}."}]})
        return httpx.Response(200, json={})

    return handler


def test_parse_playit_address():
    assert playit.parse_address("abc.joinmc.link") == ("abc.joinmc.link", None)
    assert playit.parse_address("Host.Ply.gg:12345") == ("host.ply.gg", 12345)
    assert playit.parse_address("147.185.221.3:40000") == ("147.185.221.3", 40000)
    with pytest.raises(playit.PlayitError):
        playit.parse_address("not an address")
    with pytest.raises(playit.PlayitError):
        playit.parse_address("x.joinmc.link:99999")


def test_playit_key_runs_agent(env):
    _, client, docker = env
    connect(client)
    r = client.put("/api/cloudflare/playit", json={"secret_key": "secret-abc"})
    assert r.status_code == 200, r.text
    assert r.json()["playit_key_set"] is True and r.json()["playit_status"] == "running"
    c = docker.containers.get("mcsm_playit")
    assert c.kwargs["environment"] == {"SECRET_KEY": "secret-abc"} and c.kwargs["network_mode"] == "host"
    client.put("/api/cloudflare/playit", json={"secret_key": ""})
    assert "mcsm_playit" not in {c.name for c in docker.containers.list(all=True)}


@respx.mock
def test_playit_route_follows_joinmc_srv(env):
    fake, client, _ = env
    respx.get(DOH).mock(side_effect=doh_srv("sea-route.ply.gg", 41234))
    connect(client, auto=False)
    client.post("/api/servers", json=modrinth_req(name="Survival"))
    r = client.put("/api/cloudflare/routes/survival", json={"mode": "playit", "playit_address": "abc-def.joinmc.link"})
    assert r.status_code == 200, r.text
    assert r.json()["connect_address"] == "survival.example.com"
    assert r.json()["target"] == "sea-route.ply.gg:41234"
    cname = next(x for x in fake.records.values() if x["type"] == "CNAME")
    srv = next(x for x in fake.records.values() if x["type"] == "SRV")
    assert cname["content"] == "sea-route.ply.gg" and cname["proxied"] is False
    assert srv["name"] == "_minecraft._tcp.survival.example.com"
    assert srv["data"] == {"priority": 0, "weight": 5, "port": 41234, "target": "sea-route.ply.gg"}

    # playit moves the tunnel: the next sync re-points the records.
    respx.get(DOH).mock(side_effect=doh_srv("new-route.ply.gg", 40001))
    client.post("/api/cloudflare/sync")
    srv = next(x for x in fake.records.values() if x["type"] == "SRV")
    assert srv["data"]["target"] == "new-route.ply.gg" and srv["data"]["port"] == 40001
    assert len(fake.records) == 2


def test_playit_route_with_ip_and_port(env):
    fake, client, _ = env
    connect(client, auto=False)
    client.post("/api/servers", json=modrinth_req(name="Creative"))
    r = client.put(
        "/api/cloudflare/routes/creative",
        json={"subdomain": "build", "mode": "playit", "playit_address": "147.185.221.3:40000"},
    )
    assert r.status_code == 200, r.text
    a = next(x for x in fake.records.values() if x["type"] == "A")
    srv = next(x for x in fake.records.values() if x["type"] == "SRV")
    assert a["name"] == "build.example.com" and a["content"] == "147.185.221.3"
    assert srv["data"]["target"] == "build.example.com" and srv["data"]["port"] == 40000


def test_playit_route_needs_address(env):
    _, client, _ = env
    connect(client, auto=False)
    client.post("/api/servers", json=modrinth_req(name="Lobby"))
    r = client.put("/api/cloudflare/routes/lobby", json={"mode": "playit"})
    assert r.status_code == 400 and "playit" in r.json()["detail"]


def test_auto_route_skips_playit_default(env):
    fake, client, _ = env
    connect(client, mode="playit")
    client.post("/api/servers", json=modrinth_req(name="Quiet"))
    assert client.get("/api/cloudflare/routes").json() == []
    assert fake.records == {}


def test_playit_works_without_cloudflare(env):
    fake, client, docker = env
    r = client.put("/api/cloudflare/playit", json={"secret_key": "secret-abc"})
    assert r.status_code == 200 and r.json()["playit_status"] == "running"
    assert r.json()["token_set"] is False
    client.post("/api/servers", json=modrinth_req(name="Survival"))
    r = client.put("/api/cloudflare/routes/survival", json={"playit_address": "abc-def.joinmc.link"})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "playit" and r.json()["connect_address"] == "abc-def.joinmc.link"
    assert fake.records == {}
    # Other modes still need Cloudflare.
    assert client.put("/api/cloudflare/routes/survival", json={"mode": "direct"}).status_code == 400
    assert client.delete("/api/servers/survival").status_code == 204
    assert client.get("/api/cloudflare/routes").json() == []


@respx.mock
def test_disconnect_keeps_playit(env):
    fake, client, docker = env
    respx.get(DOH).mock(side_effect=doh_srv("sea-route.ply.gg", 41234))
    connect(client, auto=False)
    client.put("/api/cloudflare/playit", json={"secret_key": "secret-abc"})
    client.post("/api/servers", json=modrinth_req(name="Survival"))
    client.put("/api/cloudflare/routes/survival", json={"mode": "playit", "playit_address": "abc.joinmc.link"})
    assert client.delete("/api/cloudflare").status_code == 204
    assert fake.records == {}
    status = client.get("/api/cloudflare").json()
    assert status["token_set"] is False and status["playit_key_set"] is True
    (route,) = client.get("/api/cloudflare/routes").json()
    assert route["connect_address"] == "abc.joinmc.link"
    assert "mcsm_playit" in {c.name for c in docker.containers.list(all=True)}
