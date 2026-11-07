"""World backup tests, reusing the fake Docker SDK from test_servers."""

import dataclasses
import json
import os
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.routers.backups import get_backups
from app.routers.servers import get_manager
from app.services import backups as backups_mod
from app.services.backups import BackupManager, BackupSchedule
from app.services.docker_manager import DockerManager

from .test_servers import FakeContainer, FakeDocker, modrinth_req


@pytest.fixture
def env(tmp_path, monkeypatch):
    data_dir = tmp_path / "app"
    s = dataclasses.replace(
        Settings(),
        data_dir=str(data_dir),
        data_root=str(data_dir / "servers"),
        host_data_root="/mnt/user/appdata/mcsm/servers",
        backup_dir=str(tmp_path / "backups"),
        backup_flush_wait=0,
    )
    monkeypatch.setattr("app.settings_store.settings", s)
    rcon_calls = []

    def exec_run(self, cmd):
        rcon_calls.append(cmd[1:])
        return SimpleNamespace(exit_code=0, output=b"ok")

    monkeypatch.setattr(FakeContainer, "exec_run", exec_run, raising=False)
    docker = FakeDocker()
    mgr = DockerManager(s, client=docker)
    bm = BackupManager(mgr, s)
    original_backup, original_restore = bm.start_backup, bm.start_restore
    bm.start_backup = lambda *a, background=True, **k: original_backup(*a, background=False, **k)
    bm.start_restore = lambda *a, background=True, **k: original_restore(*a, background=False, **k)
    original_create = mgr.create_server
    mgr.create_server = lambda req, background=True: original_create(req, background=False)
    app.dependency_overrides[get_manager] = lambda: mgr
    app.dependency_overrides[get_backups] = lambda: bm
    client = TestClient(app)
    yield SimpleNamespace(client=client, mgr=mgr, bm=bm, docker=docker, settings=s, rcon=rcon_calls, tmp=tmp_path)
    app.dependency_overrides.clear()


def make_server(env, with_world=True):
    r = env.client.post("/api/servers", json=modrinth_req())
    assert r.status_code == 202, r.text
    data = os.path.join(env.settings.data_root, "my-pack")
    if with_world:
        os.makedirs(os.path.join(data, "world", "region"))
        with open(os.path.join(data, "world", "level.dat"), "w") as f:
            f.write("level-v1")
        with open(os.path.join(data, "world", "region", "r.0.0.mca"), "w") as f:
            f.write("chunks-v1")
        with open(os.path.join(data, "world", "session.lock"), "w") as f:
            f.write("lock")
        os.makedirs(os.path.join(data, "world_nether"))
        with open(os.path.join(data, "world_nether", "level.dat"), "w") as f:
            f.write("nether")
        with open(os.path.join(data, "whitelist.json"), "w") as f:
            f.write("[]")
        os.makedirs(os.path.join(data, "mods"))
        with open(os.path.join(data, "mods", "a.jar"), "w") as f:
            f.write("jar")
        os.makedirs(os.path.join(data, "logs"))
        with open(os.path.join(data, "logs", "latest.log"), "w") as f:
            f.write("log")
    return data


def container(env):
    return env.docker.containers.items["mcsm-my-pack"]


def test_manual_backup_of_running_server_pauses_saving(env):
    make_server(env)
    assert container(env).status == "running"
    r = env.client.post("/api/servers/my-pack/backups", json={})
    assert r.status_code == 202, r.text
    assert env.rcon == [["save-off"], ["save-all", "flush"], ["save-on"]]

    o = env.client.get("/api/servers/my-pack/backups").json()
    assert o["job"] is None
    assert o["last_result"]["ok"], o["last_result"]
    assert len(o["backups"]) == 1
    b = o["backups"][0]
    assert b["kind"] == "manual" and b["scope"] == "world" and b["consistent"]

    path = os.path.join(env.settings.backup_dir, "my-pack", b["id"])
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("mcsm-backup.json"))
    assert "world/level.dat" in names
    assert "world/region/r.0.0.mca" in names
    assert "world_nether/level.dat" in names
    assert "whitelist.json" in names
    assert "world/session.lock" not in names
    assert not any(n.startswith(("mods/", "logs/")) for n in names)
    assert manifest["server_id"] == "my-pack"


def test_full_backup_includes_mods_but_not_logs(env):
    make_server(env)
    env.client.post("/api/servers/my-pack/backups", json={"scope": "full"})
    b = env.client.get("/api/servers/my-pack/backups").json()["backups"][0]
    with zipfile.ZipFile(os.path.join(env.settings.backup_dir, "my-pack", b["id"])) as z:
        names = set(z.namelist())
    assert "mods/a.jar" in names
    assert not any(n.startswith("logs/") for n in names)


def test_stopped_server_backup_skips_rcon(env):
    make_server(env)
    env.client.post("/api/servers/my-pack/stop")
    env.client.post("/api/servers/my-pack/backups")
    assert env.rcon == []
    assert len(env.client.get("/api/servers/my-pack/backups").json()["backups"]) == 1


def test_rcon_failure_still_backs_up_but_flags_it(env, monkeypatch):
    make_server(env)
    monkeypatch.setattr(
        FakeContainer, "exec_run", lambda self, cmd: SimpleNamespace(exit_code=1, output=b"connection refused")
    )
    env.client.post("/api/servers/my-pack/backups")
    o = env.client.get("/api/servers/my-pack/backups").json()
    assert o["backups"][0]["consistent"] is False
    assert "inconsistent" in o["last_result"]["message"]


def test_no_world_yet_reports_error(env):
    make_server(env, with_world=False)
    env.client.post("/api/servers/my-pack/backups")
    o = env.client.get("/api/servers/my-pack/backups").json()
    assert o["backups"] == []
    assert o["last_result"]["ok"] is False
    assert "No world folder" in o["last_result"]["message"]


def test_restore_replaces_world_and_keeps_safety_copy(env):
    data = make_server(env)
    env.client.post("/api/servers/my-pack/backups")
    backup_id = env.client.get("/api/servers/my-pack/backups").json()["backups"][0]["id"]

    # The world changes after the backup.
    with open(os.path.join(data, "world", "region", "r.0.0.mca"), "w") as f:
        f.write("chunks-v2-griefed")
    with open(os.path.join(data, "world", "region", "r.1.0.mca"), "w") as f:
        f.write("new region")

    stops = []
    c = container(env)
    orig_stop = c.stop
    c.stop = lambda timeout=10: (stops.append(timeout), orig_stop(timeout))
    r = env.client.post(f"/api/servers/my-pack/backups/{backup_id}/restore")
    assert r.status_code == 202, r.text
    o = env.client.get("/api/servers/my-pack/backups").json()
    assert o["last_result"]["ok"], o["last_result"]

    with open(os.path.join(data, "world", "region", "r.0.0.mca")) as f:
        assert f.read() == "chunks-v1"
    assert not os.path.exists(os.path.join(data, "world", "region", "r.1.0.mca"))
    # Mods and logs are not part of a world backup and are left alone.
    assert os.path.exists(os.path.join(data, "mods", "a.jar"))
    assert not os.path.exists(os.path.join(data, ".mcsm-restore-tmp"))
    # The server was stopped for the restore and started again.
    assert stops and c.status == "running"
    kinds = sorted(b["kind"] for b in o["backups"])
    assert kinds == ["manual", "pre-restore"]
    assert "previous world was saved" in o["last_result"]["message"]


def test_restore_rejects_zip_slip(env):
    make_server(env)
    folder = os.path.join(env.settings.backup_dir, "my-pack")
    os.makedirs(folder)
    name = "my-pack_20260101-000000_manual.zip"
    with zipfile.ZipFile(os.path.join(folder, name), "w") as z:
        z.writestr("../../evil.txt", "x")
    env.client.post(f"/api/servers/my-pack/backups/{name}/restore")
    o = env.client.get("/api/servers/my-pack/backups").json()
    assert o["last_result"]["ok"] is False
    assert "unsafe path" in o["last_result"]["message"]
    assert not (env.tmp / "evil.txt").exists()


def test_download_and_delete(env):
    make_server(env)
    env.client.post("/api/servers/my-pack/backups")
    backup_id = env.client.get("/api/servers/my-pack/backups").json()["backups"][0]["id"]
    r = env.client.get(f"/api/servers/my-pack/backups/{backup_id}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert backup_id in r.headers["content-disposition"]
    assert r.content[:2] == b"PK"

    assert env.client.delete(f"/api/servers/my-pack/backups/{backup_id}").status_code == 204
    assert env.client.get("/api/servers/my-pack/backups").json()["backups"] == []
    assert env.client.delete(f"/api/servers/my-pack/backups/{backup_id}").status_code == 404


def test_bad_backup_ids_are_404(env):
    make_server(env)
    assert env.client.get("/api/servers/my-pack/backups/..%2F..%2Fsettings.json/download").status_code == 404
    assert env.client.delete("/api/servers/my-pack/backups/other_20260101-000000_manual.zip").status_code == 404
    assert env.client.get("/api/servers/nope/backups").status_code == 404


def fake_backup(folder, server_id, when, kind):
    os.makedirs(folder, exist_ok=True)
    name = f"{server_id}_{when.strftime('%Y%m%d-%H%M%S')}_{kind}.zip"
    with zipfile.ZipFile(os.path.join(folder, name), "w") as z:
        z.writestr("mcsm-backup.json", json.dumps({"scope": "world"}))
    return name


def test_retention_keeps_newest_automatic_and_all_manual(env):
    make_server(env)
    folder = os.path.join(env.settings.backup_dir, "my-pack")
    now = datetime.now(UTC)
    auto = [fake_backup(folder, "my-pack", now - timedelta(hours=h), "scheduled") for h in range(1, 6)]
    manual = fake_backup(folder, "my-pack", now - timedelta(days=30), "manual")
    r = env.client.put("/api/servers/my-pack/backups/schedule", json={"enabled": False, "keep": 2})
    assert r.status_code == 200, r.text
    left = {b["id"] for b in env.client.get("/api/servers/my-pack/backups").json()["backups"]}
    assert left == {auto[0], auto[1], manual}


def test_retention_by_age_keeps_at_least_one(env):
    make_server(env)
    folder = os.path.join(env.settings.backup_dir, "my-pack")
    now = datetime.now(UTC)
    old = [fake_backup(folder, "my-pack", now - timedelta(days=d), "scheduled") for d in (10, 20)]
    env.bm.set_schedule("my-pack", BackupSchedule(keep=10, keep_days=7))
    left = {b.id for b in env.bm.list_backups("my-pack")}
    assert left == {old[0]}


def test_scheduler_runs_due_backups(env):
    make_server(env)
    env.bm.set_schedule("my-pack", BackupSchedule(enabled=True, interval_minutes=60))
    assert env.bm.run_due() == ["my-pack"]
    # Not due again until the interval passes.
    assert env.bm.run_due() == []
    backups = env.bm.list_backups("my-pack")
    assert [b.kind for b in backups] == ["scheduled"]
    nxt = env.client.get("/api/servers/my-pack/backups").json()["next_scheduled_at"]
    assert datetime.fromisoformat(nxt) > datetime.now(UTC) + timedelta(minutes=55)


def test_scheduler_skips_unchanged_stopped_server(env):
    make_server(env)
    env.client.post("/api/servers/my-pack/stop")
    folder = os.path.join(env.settings.backup_dir, "my-pack")
    # A recent manual backup newer than any world change, and an old scheduled one.
    fake_backup(folder, "my-pack", datetime.now(UTC) - timedelta(hours=5), "scheduled")
    fake_backup(folder, "my-pack", datetime.now(UTC) + timedelta(minutes=1), "manual")
    env.bm.set_schedule("my-pack", BackupSchedule(enabled=True, interval_minutes=60))
    assert env.bm.run_due() == []


def test_schedule_validation(env):
    make_server(env)
    assert env.client.put("/api/servers/my-pack/backups/schedule", json={"interval_minutes": 1}).status_code == 422
    assert env.client.put("/api/servers/my-pack/backups/schedule", json={"scope": "mods"}).status_code == 422
    r = env.client.put("/api/servers/my-pack/backups/schedule", json={"enabled": True, "interval_minutes": 720})
    assert r.json()["interval_minutes"] == 720
    assert env.client.get("/api/servers/my-pack/backups").json()["schedule"]["enabled"] is True


def test_backup_location_setting(env):
    loc = env.client.get("/api/backups/location").json()
    assert loc["source"] == "env" and loc["writable"]

    new = str(env.tmp / "share" / "mc-backups")
    r = env.client.put("/api/backups/location", json={"path": new})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == new and r.json()["source"] == "settings"
    assert env.bm.root == new

    assert env.client.put("/api/backups/location", json={"path": "relative/dir"}).status_code == 400
    inside = os.path.join(env.settings.data_root, "x")
    assert env.client.put("/api/backups/location", json={"path": inside}).status_code == 400

    r = env.client.put("/api/backups/location", json={"path": ""})
    assert r.json()["source"] == "env"


def test_default_location_without_env(env, monkeypatch):
    s = dataclasses.replace(env.settings, backup_dir="")
    monkeypatch.setattr(backups_mod, "DEFAULT_BACKUP_MOUNT", str(env.tmp / "no-such-mount"))
    bm = BackupManager(env.mgr, s)
    assert bm.root == os.path.join(s.data_dir, "backups")
