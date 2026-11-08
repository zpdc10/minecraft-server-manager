"""Modpack update tests: version checks, saved CurseForge links, and safe updates with rollback."""

import asyncio
import dataclasses
import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.models.cf_import import ImportFile, ImportPreview
from app.models.modpack import PackVersion
from app.models.server import CreateServerRequest, ServerInfo
from app.models.updates import UpdateVersion
from app.providers import PROVIDERS
from app.routers.servers import get_manager
from app.routers.updates import get_updates
from app.services import cf_import, pack_updates, server_builder
from app.services import mod_db as mod_db_module
from app.services.backups import BackupManager
from app.services.docker_manager import DockerManager
from app.services.pack_updates import UpdateManager, pick_newer, recommend

from .test_builder import DONE, ScriptedDocker, forge_toml, make_jar


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = dataclasses.replace(
        Settings(),
        data_dir=str(tmp_path / "app"),
        data_root=str(tmp_path / "servers"),
        host_data_root="/mnt/user/appdata/mc-servers",
        backup_dir=str(tmp_path / "backups"),
        backup_flush_wait=0,
        mod_db_url="",
        build_max_attempts=3,
    )
    monkeypatch.setattr("app.settings_store.settings", s)
    monkeypatch.setattr(server_builder, "POLL_SECONDS", 0)
    monkeypatch.setattr(mod_db_module, "_instance", None)
    return s


def info(**kw):
    base = dict(
        id="p",
        name="P",
        source="modrinth",
        port=25565,
        memory_gb=4,
        java_version="java17",
        status="running",
        container_name="mcsm-p",
        data_path="/x",
        minecraft_version="1.20.1",
        loader="forge",
        created_at="2026-01-01T00:00:00+00:00",
    )
    return ServerInfo(**{**base, **kw})


def v(id, mc="1.20.1", loader="forge", published="2026-01-01T00:00:00Z", rt="release"):
    return UpdateVersion(
        id=id, name=f"Pack {id}", minecraft_version=mc, loader=loader, published=published, release_type=rt
    )


# ---- picking versions ------------------------------------------------------------------


def test_newer_than_current_and_recommendation():
    versions = [v("5", mc="1.21.1"), v("4", rt="beta"), v("3"), v("2", loader="fabric"), v("1")]
    newer = pick_newer(versions, "1", info())
    # The Fabric build of the pack is left out for a Forge server.
    assert [x.id for x in newer] == ["5", "4", "3"]
    assert newer[0].changes_minecraft and not newer[1].changes_minecraft
    # Newest release for the same Minecraft version wins over a beta and over a Minecraft jump.
    assert recommend(newer).id == "3"
    assert pick_newer(versions, "5", info()) == []


def test_unknown_current_uses_creation_date():
    versions = [v("3", published="2026-03-01T00:00:00Z"), v("2", published="2025-12-01T00:00:00Z")]
    newer = pick_newer(versions, "", info(created_at="2026-01-01T00:00:00+00:00"))
    assert [x.id for x in newer] == ["3"]


def test_modrinth_check(settings, monkeypatch):
    async def get_versions(project_id):
        assert project_id == "abc"
        return [
            PackVersion(
                id="new",
                name="1.1",
                version_number="1.1",
                game_versions=["1.20.1"],
                loaders=["forge"],
                published="2026-05-01T00:00:00Z",
            ),
            PackVersion(
                id="old",
                name="1.0",
                version_number="1.0",
                game_versions=["1.20.1"],
                loaders=["forge"],
                published="2026-01-01T00:00:00Z",
            ),
        ]

    monkeypatch.setattr(PROVIDERS["modrinth"], "get_versions", get_versions)
    mgr = DockerManager(settings, client=ScriptedDocker(lambda c: None))
    um = UpdateManager(mgr, None)
    check = asyncio.run(um._check(info(project_id="abc", version_id="old")))
    assert check.update_available and check.latest.id == "new"
    assert check.current_name == "1.0"


def test_modrinth_latest_reads_itzg_manifest(settings, tmp_path):
    mgr = DockerManager(settings, client=ScriptedDocker(lambda c: None))
    os.makedirs(mgr.local_data_path("p"))
    with open(os.path.join(mgr.local_data_path("p"), ".modrinth-manifest.json"), "w") as f:
        f.write('{"versionId": "abc123"}')
    assert UpdateManager(mgr, None)._itzg_version(info()) == "abc123"


# ---- imported CurseForge packs: saved links -------------------------------------------


def preview(project_id="999", files=("30", "20", "10")):
    return ImportPreview(
        project_id=project_id,
        slug="cool-pack",
        title="Cool Pack",
        url="https://www.curseforge.com/minecraft/modpacks/cool-pack",
        files=[
            ImportFile(id=f, display_name=f"Cool Pack 1.{f}", minecraft_version="1.20.1", loader="forge") for f in files
        ],
    )


def test_link_saved_when_imported_server_is_made(settings):
    mgr = DockerManager(settings, client=ScriptedDocker(lambda c: None))
    um = UpdateManager(mgr, None)
    s = info(source="cfimport", project_id="999", project_slug="cool-pack", version_id="10")
    um.remember_created(s, "https://www.curseforge.com/minecraft/modpacks/cool-pack/files/10")
    link = UpdateManager(mgr, None).get_link(s)
    assert link.project_id == "999"
    assert link.url.endswith("/files/10")


def test_old_import_without_saved_link_still_checks(settings, monkeypatch):
    async def fake_preview(text):
        assert text == "999"
        return preview()

    monkeypatch.setattr(cf_import, "preview", fake_preview)
    mgr = DockerManager(settings, client=ScriptedDocker(lambda c: None))
    check = asyncio.run(
        UpdateManager(mgr, None)._check(
            info(source="cfimport", project_id="999", project_slug="cool-pack", version_id="10")
        )
    )
    assert [x.id for x in check.newer] == ["30", "20"]
    assert check.link.project_id == "999"


def test_uploaded_pack_needs_link_then_matches_manifest(settings, monkeypatch):
    mgr = DockerManager(settings, client=ScriptedDocker(lambda c: None))
    um = UpdateManager(mgr, None)
    s = info(source="cfimport", project_id="upload-0123456789ab", version_id="upload-0123456789ab", pack_title="Odd")
    monkeypatch.setattr(mgr, "get_server", lambda sid: s)
    monkeypatch.setattr(um, "_upload_manifest", lambda i: {"name": "Odd Name", "version": "1.20"})

    async def not_found(text):
        raise cf_import.ProviderError("nope", status_code=404)

    monkeypatch.setattr(cf_import, "preview", not_found)
    check = asyncio.run(um.check("p"))
    assert not check.supported and "Paste the pack's CurseForge link" in check.reason

    async def found(text):
        return preview()

    monkeypatch.setattr(cf_import, "preview", found)
    check = asyncio.run(um.link_pack("p", "https://www.curseforge.com/minecraft/modpacks/cool-pack"))
    assert check.supported and check.link.project_id == "999"
    # The upload's manifest says version 1.20, so file 20 is the current one.
    assert check.current_version_id == "20"
    assert [x.id for x in check.newer] == ["30"]


# ---- the update itself ---------------------------------------------------------------


def pack_behavior(mods_dir, broken_versions=()):
    """A Modrinth pack: the version in MODRINTH_VERSION decides which mods get installed."""

    def behavior(c):
        version = c.env.get("MODRINTH_VERSION", "v1")
        if c.env.get("SETUP_ONLY") == "true":
            os.makedirs(mods_dir, exist_ok=True)
            for f in os.listdir(mods_dir):
                os.remove(os.path.join(mods_dir, f))
            make_jar(os.path.join(mods_dir, f"core-{version}.jar"), forge=forge_toml("core"))
            c.status, c.exit_code = "exited", 0
        elif version in broken_versions:
            c.status, c.exit_code = "exited", 1
            c.log = "java.lang.IllegalStateException: the new version is broken\n"
        else:
            c.log = DONE

    return behavior


def setup_server(settings, broken=()):
    mods_dir = os.path.join(settings.data_root, "pack", "mods")
    docker = ScriptedDocker(pack_behavior(mods_dir, broken))
    mgr = DockerManager(settings, client=docker)
    req = CreateServerRequest(
        name="Pack",
        source="modrinth",
        project_id="abc",
        project_slug="pack",
        version_id="v1",
        minecraft_version="1.20.1",
        loader="forge",
    )
    mgr.create_server(req, background=False)
    data = mgr.local_data_path("pack")
    os.makedirs(os.path.join(data, "world"))
    with open(os.path.join(data, "world", "level.dat"), "w") as f:
        f.write("world-v1")
    um = UpdateManager(mgr, BackupManager(mgr, settings))
    check = pack_updates.UpdateCheck(server_id="pack", current_name="1.0", newer=[v("v2")])
    return mgr, um, check, mods_dir, data


def test_update_passes_and_keeps_new_version(settings):
    mgr, um, check, mods_dir, data = setup_server(settings)
    assert mgr.get_server("pack").status == "running"
    um.start_update("pack", check, "v2", background=False)
    s = mgr.get_server("pack")
    assert s.version_id == "v2" and s.status == "running" and not s.busy
    assert os.listdir(mods_dir) == ["core-v2.jar"]
    job = um.history("pack")[0]
    assert job.phase == "passed", job.message
    # A full backup was taken first.
    backups = um.backups.list_backups("pack")
    assert [b.kind for b in backups] == ["pre-update"] and backups[0].scope == "full"
    assert job.backup_id == backups[0].id


def test_failed_update_rolls_back(settings):
    mgr, um, check, mods_dir, data = setup_server(settings, broken=("v2",))
    um.start_update("pack", check, "v2", background=False)
    job = um.history("pack")[0]
    assert job.phase == "rolled-back", job.message
    assert "the new version is broken" in job.message
    assert job.build is not None and job.build.phase == "failed"
    s = mgr.get_server("pack")
    # Back on the old version, with the old mods and the old build report, running again.
    assert s.version_id == "v1"
    assert os.listdir(mods_dir) == ["core-v1.jar"]
    assert s.status == "running"
    assert s.build.phase == "passed"
    with open(os.path.join(data, "world", "level.dat")) as f:
        assert f.read() == "world-v1"


def test_stopped_server_stays_stopped(settings):
    mgr, um, check, *_ = setup_server(settings)
    mgr.stop_server("pack")
    um.start_update("pack", check, "v2", background=False)
    s = mgr.get_server("pack")
    assert s.version_id == "v2" and s.status == "stopped"


def test_update_rejects_unknown_version(settings):
    mgr, um, check, *_ = setup_server(settings)
    with pytest.raises(pack_updates.UpdateError):
        um.start_update("pack", check, "v9", background=False)


def test_api_lists_updates(settings, monkeypatch):
    mgr, um, check, *_ = setup_server(settings)

    async def get_versions(project_id):
        return [
            PackVersion(id="v2", name="2.0", version_number="2.0", game_versions=["1.20.1"], loaders=["forge"]),
            PackVersion(id="v1", name="1.0", version_number="1.0", game_versions=["1.20.1"], loaders=["forge"]),
        ]

    monkeypatch.setattr(PROVIDERS["modrinth"], "get_versions", get_versions)
    app.dependency_overrides[get_manager] = lambda: mgr
    app.dependency_overrides[get_updates] = lambda: um
    try:
        client = TestClient(app)
        r = client.get("/api/updates")
        assert r.status_code == 200, r.text
        [row] = r.json()
        assert row["check"]["update_available"] and row["check"]["latest"]["id"] == "v2"
        r = client.get("/api/servers/pack/updates")
        assert r.json()["check"]["current_name"] == "1.0"
        r = client.put("/api/servers/pack/updates/link", json={"url": "x"})
        assert r.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_imported_pack_update_clears_old_mods_and_downloads_new_file(settings, monkeypatch):
    calls = []

    def fake_install(project_id, file_id, data_dir, uid=None, gid=None, progress=lambda m: None, **kw):
        calls.append((project_id, file_id))
        mods = os.path.join(data_dir, "mods")
        os.makedirs(mods, exist_ok=True)
        make_jar(os.path.join(mods, f"core-{file_id}.jar"), forge=forge_toml("core"))
        return {"TYPE": "FORGE", "VERSION": "1.20.1", "FORGE_VERSION": f"47.{file_id}"}, [], False

    monkeypatch.setattr(cf_import, "install", fake_install)

    def behavior(c):
        if c.env.get("SETUP_ONLY") == "true":
            c.status, c.exit_code = "exited", 0
        else:
            c.log = DONE

    docker = ScriptedDocker(behavior)
    mgr = DockerManager(settings, client=docker)
    req = CreateServerRequest(
        name="Cool",
        source="cfimport",
        project_id="999",
        project_slug="cool-pack",
        version_id="10",
        minecraft_version="1.20.1",
        loader="forge",
    )
    mgr.create_server(req, background=False)
    mods = mgr.mods_dir("cool")
    assert os.listdir(mods) == ["core-10.jar"]
    um = UpdateManager(mgr, BackupManager(mgr, settings))
    um.remember_created(mgr.get_server("cool"), "https://www.curseforge.com/minecraft/modpacks/cool-pack")
    check = pack_updates.UpdateCheck(server_id="cool", newer=[v("30")], link=um.get_link(mgr.get_server("cool")))
    um.start_update("cool", check, "30", background=False)
    assert um.history("cool")[0].phase == "passed", um.history("cool")[0].message
    assert calls == [("999", "10"), ("999", "30")]
    assert os.listdir(mods) == ["core-30.jar"]
    s = mgr.get_server("cool")
    assert s.version_id == "30"
    env = docker.created[-1].env
    assert env["FORGE_VERSION"] == "47.30"
    # The saved link survives the update.
    assert um.get_link(s).url.endswith("/cool-pack")
