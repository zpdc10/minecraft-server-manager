"""Tests for importing CurseForge packs from a link or an upload, without an API key."""

import dataclasses
import io
import json
import zipfile

import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.models.server import CreateServerRequest
from app.providers import ProviderError
from app.services import cf_import
from app.services.docker_manager import DockerManager
from app.services.itzg_env import EnvBuildError, build_env

from .test_servers import FakeDocker

SITE = "https://www.curseforge.com/api/v1"
CDN = "https://mediafilez.forgecdn.net/files"
WIDGET = "https://api.cfwidget.com"
MR = "https://api.modrinth.com/v2"

client = TestClient(app)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cf_import, "settings", dataclasses.replace(Settings(), data_dir=str(tmp_path / "app")))
    return tmp_path


def pack_zip(files=((100, 1001), (200, 2002), (300, 3003), (400, 4004)), loader="forge-47.3.0", extra=None) -> bytes:
    manifest = {
        "minecraft": {"version": "1.20.1", "modLoaders": [{"id": loader, "primary": True}]},
        "manifestType": "minecraftModpack",
        "name": "Test Pack",
        "version": "1.0",
        "author": "someone",
        "files": [{"projectID": p, "fileID": f, "required": True} for p, f in files],
        "overrides": "overrides",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("overrides/config/test.toml", "a=1")
        z.writestr("overrides/../evil.txt", "nope")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def site_file(pid, fid, name, versions=("1.20.1", "Forge"), **kw):
    return {"data": {"id": fid, "projectId": pid, "fileName": name, "gameVersions": list(versions), **kw}}


# ---- links ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,slug,pid,fid",
    [
        ("https://www.curseforge.com/minecraft/modpacks/all-the-mods-9", "all-the-mods-9", "", ""),
        ("https://www.curseforge.com/minecraft/modpacks/all-the-mods-9/files/7097953", "all-the-mods-9", "", "7097953"),
        ("curseforge.com/minecraft/modpacks/ATM9/download/123?x=1", "atm9", "", "123"),
        ("https://www.curseforge.com/projects/715572", "", "715572", ""),
        ("715572", "", "715572", ""),
        ("all-the-mods-9", "all-the-mods-9", "", ""),
    ],
)
def test_parse_link(text, slug, pid, fid):
    link = cf_import.parse_link(text)
    assert (link.slug, link.project_id, link.file_id) == (slug, pid, fid)


def test_parse_bad_link():
    with pytest.raises(ProviderError):
        cf_import.parse_link("https://example.com/not a pack")


def test_cdn_url():
    assert cf_import.cdn_url(7097953, "All the Mods 9-1.1.1.zip") == f"{CDN}/7097/953/All%20the%20Mods%209-1.1.1.zip"
    assert cf_import.cdn_url(4004, "a.jar") == f"{CDN}/4/4/a.jar"


def test_preview_link():
    widget = {
        "id": 715572,
        "title": "All the Mods 9",
        "summary": "lots of mods",
        "type": "Modpacks",
        "thumbnail": "https://img/x.png",
        "urls": {"curseforge": "https://www.curseforge.com/minecraft/modpacks/all-the-mods-9"},
        "files": [
            {
                "id": 10,
                "name": "a-1.0.zip",
                "display": "A 1.0",
                "type": "release",
                "version": "1.20.1",
                "versions": ["1.20.1", "Forge"],
            },
            {
                "id": 12,
                "name": "a-1.2.zip",
                "display": "A 1.2 beta",
                "type": "beta",
                "version": "1.20.1",
                "versions": ["1.20.1", "NeoForge"],
            },
            {
                "id": 11,
                "name": "a-1.1.zip",
                "display": "A 1.1",
                "type": "release",
                "version": "1.20.1",
                "versions": ["1.20.1", "Forge"],
            },
        ],
    }
    with respx.mock() as mock:
        mock.get(f"{WIDGET}/minecraft/modpacks/all-the-mods-9").respond(json=widget)
        mock.get(f"{SITE}/mods/715572/files/11").respond(json=site_file(715572, 11, "a-1.1.zip", hasServerPack=True))
        r = client.post("/api/imports/curseforge/preview", json={"url": widget["urls"]["curseforge"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == "715572"
    assert [f["id"] for f in body["files"]] == ["12", "11", "10"]
    assert body["files"][0]["loader"] == "neoforge"
    # The newest release, not the newer beta.
    assert body["selected_file_id"] == "11"
    assert body["selected_has_server_pack"] is True


def test_preview_queued_and_missing():
    with respx.mock() as mock:
        mock.get(f"{WIDGET}/minecraft/modpacks/new-pack").respond(202, json={"title": "In queue"})
        mock.get(f"{WIDGET}/minecraft/modpacks/gone").respond(404, json={"error": "not_found"})
        assert client.post("/api/imports/curseforge/preview", json={"url": "new-pack"}).status_code == 503
        assert client.post("/api/imports/curseforge/preview", json={"url": "gone"}).status_code == 404


# ---- manifest + env -----------------------------------------------------------------


def test_parse_manifest_and_env():
    spec = cf_import.parse_manifest(
        {
            "minecraft": {"version": "1.21.1", "modLoaders": [{"id": "neoforge-21.1.77", "primary": True}]},
            "files": [{"projectID": 1, "fileID": 2}, {"projectID": 3, "fileID": 4, "required": False}],
        }
    )
    assert spec.files == [(1, 2)]
    assert cf_import.loader_env(spec) == {"TYPE": "NEOFORGE", "VERSION": "1.21.1", "NEOFORGE_VERSION": "21.1.77"}


def test_parse_manifest_without_loader():
    with pytest.raises(cf_import.PackImportError):
        cf_import.parse_manifest({"minecraft": {"version": "1.20.1", "modLoaders": []}})


def test_build_env_validation():
    settings = Settings()
    req = CreateServerRequest(name="x", source="cfimport", project_id="715572", version_id="11")
    assert build_env(req, settings)["EULA"] == "TRUE"
    with pytest.raises(EnvBuildError):
        build_env(CreateServerRequest(name="x", source="cfimport", project_id="715572"), settings)
    with pytest.raises(EnvBuildError):
        build_env(CreateServerRequest(name="x", source="cfimport", project_id="../etc"), settings)


# ---- install ------------------------------------------------------------------------


def mock_client_pack(mock, has_server_pack=False):
    mock.get(f"{SITE}/mods/9/files/90").respond(json=site_file(9, 90, "Test Pack-1.0.zip"))
    mock.get(f"{CDN}/0/90/Test%20Pack-1.0.zip").respond(content=pack_zip())
    extra = (
        []
        if not has_server_pack
        else [{"id": 91, "fileName": "Server-1.0.zip", "isCompatibleWithClient": False, "parentProjectFileId": 90}]
    )
    mock.get(f"{SITE}/mods/9/files/90/additional-files").respond(json={"data": extra})


def test_install_downloads_mods(tmp_path):
    with respx.mock() as mock:
        mock_client_pack(mock)
        # A normal mod.
        mock.get(f"{SITE}/mods/100/files/1001").respond(
            json=site_file(100, 1001, "good.jar", ("1.20.1", "Client", "Server"))
        )
        mock.get(f"{CDN}/1/1/good.jar").respond(content=b"good")
        # A mod CurseForge tags client-only: skipped, not downloaded.
        mock.get(f"{SITE}/mods/200/files/2002").respond(json=site_file(200, 2002, "zoom.jar", ("1.20.1", "Client")))
        # A mod the CDN won't give: found on Modrinth by its slug.
        mock.get(f"{SITE}/mods/300/files/3003").respond(json=site_file(300, 3003, "jei.jar"))
        mock.get(f"{CDN}/3/3/jei.jar").respond(404)
        mock.get(f"{WIDGET}/300").respond(
            json={"title": "JEI", "urls": {"curseforge": "https://www.curseforge.com/minecraft/mc-mods/jei"}}
        )
        mock.get(f"{MR}/project/jei/version").respond(
            json=[
                {
                    "version_number": "15.0",
                    "files": [{"url": "https://cdn.modrinth.com/jei.jar", "filename": "jei-15.jar", "primary": True}],
                }
            ]
        )
        mock.get("https://cdn.modrinth.com/jei.jar").respond(content=b"jei")
        # A mod that can't be found anywhere.
        mock.get(f"{SITE}/mods/400/files/4004").respond(404)
        mock.get(f"{WIDGET}/400").respond(404)

        steps = []
        env, notes, used_server_pack = cf_import.install("9", "90", str(tmp_path / "srv"), progress=steps.append)

    srv = tmp_path / "srv"
    assert env == {"TYPE": "FORGE", "VERSION": "1.20.1", "FORGE_VERSION": "47.3.0"}
    assert not used_server_pack
    assert sorted(p.name for p in (srv / "mods").iterdir()) == ["good.jar", "jei-15.jar"]
    assert (srv / "config" / "test.toml").read_text() == "a=1"
    assert not (tmp_path / "evil.txt").exists()
    assert "Downloaded 2 of 4 mods." in notes
    assert any("Modrinth" in n for n in notes)
    assert any("project 400" in n for n in notes)
    assert steps[-1] == "Downloading mods (4 of 4)"
    # The downloaded pack zip isn't kept around.
    assert not list((cf_import.imports_dir() / "cache").iterdir())


def test_install_prefers_server_pack(tmp_path):
    sp = io.BytesIO()
    with zipfile.ZipFile(sp, "w") as z:
        z.writestr("Server-Files-1.0/mods/server-mod.jar", "x")
        z.writestr("Server-Files-1.0/config/a.toml", "b=2")
    with respx.mock() as mock:
        mock_client_pack(mock, has_server_pack=True)
        mock.get(f"{CDN}/0/91/Server-1.0.zip").respond(content=sp.getvalue())
        env, notes, used = cf_import.install("9", "90", str(tmp_path / "srv"))
    assert used
    assert env["TYPE"] == "FORGE"
    assert (tmp_path / "srv" / "mods" / "server-mod.jar").exists()
    assert (tmp_path / "srv" / "config" / "a.toml").exists()
    assert "server pack" in notes[0]


def test_install_bad_file(tmp_path):
    with respx.mock() as mock:
        mock.get(f"{SITE}/mods/9/files/99").respond(404)
        mock.get(f"{WIDGET}/9").respond(json={"files": []})
        with pytest.raises(cf_import.PackImportError, match="Couldn't find file"):
            cf_import.install("9", "99", str(tmp_path / "srv"))


# ---- uploads + server creation -------------------------------------------------------


def test_upload_and_build_server(tmp_path):
    r = client.put("/api/imports/upload?filename=Test Pack-1.0.zip", content=pack_zip(files=[(100, 1001)]))
    assert r.status_code == 200, r.text
    preview = r.json()
    upload_id = preview["project_id"]
    assert upload_id.startswith("upload-")
    assert preview["files"][0]["mod_count"] == 1
    assert preview["files"][0]["loader"] == "forge"

    settings = dataclasses.replace(Settings(), data_root=str(tmp_path / "servers"), host_data_root="/mnt/x")
    mgr = DockerManager(settings, client=FakeDocker())
    req = CreateServerRequest(
        name="Imported",
        source="cfimport",
        project_id=upload_id,
        version_id=upload_id,
        minecraft_version="1.20.1",
        loader="forge",
        auto_fix=False,
    )
    with respx.mock() as mock:
        mock.get(f"{SITE}/mods/100/files/1001").respond(json=site_file(100, 1001, "good.jar"))
        mock.get(f"{CDN}/1/1/good.jar").respond(content=b"good")
        info = mgr.create_server(req, background=False)
    container = mgr._get_container(info.id)
    assert container is not None
    env = container.kwargs["environment"]
    assert env["TYPE"] == "FORGE" and env["FORGE_VERSION"] == "47.3.0"
    build = mgr.get_build(info.id)
    assert build.phase == "passed"
    assert "Downloaded 1 of 1 mods." in build.notes
    # Uploads are kept so the server can be rebuilt.
    assert cf_import.upload_path(upload_id).exists()


def test_upload_rejects_non_pack():
    assert client.put("/api/imports/upload", content=b"not a zip").status_code == 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mods/a.jar", "x")
    r = client.put("/api/imports/upload", content=buf.getvalue())
    assert r.status_code == 400
    assert "manifest.json" in r.json()["detail"]


def test_failed_import_marks_server(tmp_path):
    settings = dataclasses.replace(Settings(), data_root=str(tmp_path / "servers"), host_data_root="/mnt/x")
    mgr = DockerManager(settings, client=FakeDocker())
    req = CreateServerRequest(name="Broken", source="cfimport", project_id="9", version_id="99", auto_fix=False)
    with respx.mock() as mock:
        mock.get(f"{SITE}/mods/9/files/99").respond(404)
        mock.get(f"{WIDGET}/9").respond(404)
        info = mgr.create_server(req, background=False)
    server = mgr.get_server(info.id)
    assert server.status == "error"
    assert "Importing the pack failed" in server.error
