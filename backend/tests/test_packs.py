"""Pack builder tests: dependency resolution, exports and server install, with mocked APIs."""

import hashlib
import io
import json
import zipfile

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import settings_store
from app.config import settings
from app.main import app
from app.models.pack import CustomPack
from app.models.server import CreateServerRequest
from app.providers import modrinth
from app.services import loader_versions, pack_export, pack_install, pack_store
from app.services.itzg_env import build_env

client = TestClient(app)
MR = "https://api.modrinth.com/v2"
CF = "https://api.curseforge.com"

JAR_SODIUM = b"sodium jar bytes"
JAR_API = b"fabric api jar bytes"
JAR_MENU = b"modmenu jar"


def sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def project(pid, slug, title, client_side="required", server_side="required"):
    return {
        "id": pid,
        "slug": slug,
        "title": title,
        "description": "",
        "icon_url": None,
        "downloads": 1,
        "followers": 1,
        "loaders": ["fabric"],
        "categories": [],
        "game_versions": ["1.20.1"],
        "client_side": client_side,
        "server_side": server_side,
    }


def version(vid, pid, jar, deps=(), mc="1.20.1", vtype="release", date="2026-01-01"):
    return {
        "id": vid,
        "project_id": pid,
        "name": f"{pid} {vid}",
        "version_number": vid,
        "version_type": vtype,
        "game_versions": [mc],
        "loaders": ["fabric"],
        "date_published": date,
        "dependencies": list(deps),
        "files": [
            {
                "primary": True,
                "filename": f"{pid}-{vid}.jar",
                "url": f"https://cdn.modrinth.com/data/{pid}/{vid}.jar",
                "size": len(jar),
                "hashes": {"sha1": sha1(jar), "sha512": hashlib.sha512(jar).hexdigest()},
            }
        ],
    }


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pack_store, "_dir", lambda: tmp_path / "packs")
    monkeypatch.setattr(settings_store, "_path", lambda: tmp_path / "settings.json")
    modrinth._cache.clear()
    loader_versions._cache.clear()
    return tmp_path


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://meta.fabricmc.net/v2/versions/loader/1.20.1").respond(
            json=[{"loader": {"version": "0.17.0", "stable": False}}, {"loader": {"version": "0.16.9", "stable": True}}]
        )
        mock.get(f"{MR}/project/sodium").respond(
            json=project("AANobbMI", "sodium", "Sodium", server_side="unsupported")
        )
        mock.get(f"{MR}/project/AANobbMI").respond(
            json=project("AANobbMI", "sodium", "Sodium", server_side="unsupported")
        )
        mock.get(f"{MR}/project/P7dR8mSH").respond(json=project("P7dR8mSH", "fabric-api", "Fabric API"))
        mock.get(f"{MR}/project/modmenu").respond(json=project("mOgUt4GM", "modmenu", "Mod Menu"))
        mock.get(f"{MR}/project/mOgUt4GM").respond(json=project("mOgUt4GM", "modmenu", "Mod Menu"))
        mock.get(f"{MR}/project/AANobbMI/version").respond(
            json=[
                version("sodBeta", "AANobbMI", JAR_SODIUM, vtype="beta", date="2026-03-01"),
                version(
                    "sod1",
                    "AANobbMI",
                    JAR_SODIUM,
                    deps=[{"project_id": "P7dR8mSH", "dependency_type": "required"}],
                ),
            ]
        )
        mock.get(f"{MR}/project/P7dR8mSH/version").respond(json=[version("api1", "P7dR8mSH", JAR_API)])
        # Mod Menu names its dependency by version id only.
        mock.get(f"{MR}/project/mOgUt4GM/version").respond(
            json=[version("menu1", "mOgUt4GM", JAR_MENU, deps=[{"version_id": "api1", "dependency_type": "required"}])]
        )
        mock.get(f"{MR}/versions").respond(json=[{"id": "api1", "project_id": "P7dR8mSH"}])
        yield mock


def make_pack(**kw):
    resp = client.post(
        "/api/custom-packs", json={"name": "My Pack", "minecraft_version": "1.20.1", "loader": "fabric", **kw}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_pack_picks_stable_loader(api):
    pack = make_pack()
    assert pack["id"] == "my-pack"
    assert pack["loader_version"] == "0.16.9"
    assert make_pack()["id"] == "my-pack-2"
    assert {p["id"] for p in client.get("/api/custom-packs").json()} == {"my-pack", "my-pack-2"}


def test_add_mod_resolves_dependencies(api):
    make_pack()
    r = client.post("/api/custom-packs/my-pack/mods", json={"source": "modrinth", "project_id": "sodium"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == ["Sodium", "Fabric API"]
    assert any("client-only" in n for n in body["notes"])
    mods = {m["slug"]: m for m in body["pack"]["mods"]}
    assert mods["sodium"]["version_id"] == "sod1"  # stable release preferred over newer beta
    assert mods["fabric-api"]["added_as_dependency"] is True
    assert mods["fabric-api"]["required_by"] == ["modrinth:AANobbMI"]
    assert not [i for i in body["pack"]["issues"] if i["severity"] == "error"]

    # Adding it twice is refused.
    assert (
        client.post("/api/custom-packs/my-pack/mods", json={"source": "modrinth", "project_id": "sodium"}).status_code
        == 409
    )

    # A second mod that needs Fabric API (by version id) reuses the copy already there.
    body = client.post("/api/custom-packs/my-pack/mods", json={"source": "modrinth", "project_id": "modmenu"}).json()
    assert body["added"] == ["Mod Menu"]
    api_mod = next(m for m in body["pack"]["mods"] if m["slug"] == "fabric-api")
    assert sorted(api_mod["required_by"]) == ["modrinth:AANobbMI", "modrinth:mOgUt4GM"]

    # Removing one dependent keeps the dependency; removing the last one drops it.
    pack = client.delete("/api/custom-packs/my-pack/mods/modrinth/AANobbMI").json()
    assert {m["slug"] for m in pack["mods"]} == {"modmenu", "fabric-api"}
    pack = client.delete("/api/custom-packs/my-pack/mods/modrinth/mOgUt4GM").json()
    assert pack["mods"] == []


def test_add_mod_without_compatible_version(api):
    make_pack()
    api.get(f"{MR}/project/AANobbMI/version").respond(json=[])
    r = client.post("/api/custom-packs/my-pack/mods", json={"source": "modrinth", "project_id": "sodium"})
    assert r.status_code == 400
    assert "no version for Minecraft 1.20.1" in r.json()["detail"]


def test_retarget_repicks_versions(api):
    make_pack()
    client.post("/api/custom-packs/my-pack/mods", json={"source": "modrinth", "project_id": "sodium"})
    api.get("https://meta.fabricmc.net/v2/versions/loader/1.21.1").respond(
        json=[{"loader": {"version": "0.16.9", "stable": True}}]
    )
    api.get(f"{MR}/project/AANobbMI/version").respond(
        json=[
            version(
                "sod2",
                "AANobbMI",
                JAR_SODIUM,
                mc="1.21.1",
                deps=[{"project_id": "P7dR8mSH", "dependency_type": "required"}],
            )
        ]
    )
    api.get(f"{MR}/project/P7dR8mSH/version").respond(json=[])
    r = client.put("/api/custom-packs/my-pack", json={"minecraft_version": "1.21.1"}).json()
    assert r["updated"] == ["Sodium"]
    pack = r["pack"]
    assert pack["minecraft_version"] == "1.21.1"
    assert {m["version_id"] for m in pack["mods"]} == {"sod2"}
    # Fabric API has no 1.21.1 version in this mock, so Sodium's dependency is reported missing.
    assert any(i["severity"] == "error" and "missing" in i["message"] for i in pack["issues"])


def test_check_flags_incompatible_and_wrong_version():
    pack = CustomPack(id="p", name="p", minecraft_version="1.20.1", loader="fabric", loader_version="1")
    from app.models.pack import PackMod
    from app.services.pack_resolver import check

    a = PackMod(
        source="modrinth",
        project_id="a",
        slug="a",
        title="A",
        version_id="1",
        file_name="a.jar",
        game_versions=["1.20.1"],
        loaders=["fabric"],
        incompatible=["modrinth:b"],
        download_url="x",
    )
    b = PackMod(
        source="modrinth",
        project_id="b",
        slug="b",
        title="B",
        version_id="1",
        file_name="b.jar",
        game_versions=["1.19.2"],
        loaders=["forge"],
        download_url="x",
    )
    pack.mods = [a, b]
    messages = [i.message for i in check(pack)]
    assert "A is incompatible with B." in messages
    assert any("B 1" not in m and "doesn't support" in m for m in messages)


def test_mrpack_export(api):
    make_pack()
    client.post("/api/custom-packs/my-pack/mods", json={"source": "modrinth", "project_id": "sodium"})
    r = client.get("/api/custom-packs/my-pack/export/mrpack")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="My Pack-1.0.0.mrpack"'
    assert json.loads(r.headers["x-pack-warnings"]) == []
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        index = json.loads(z.read("modrinth.index.json"))
    assert index["dependencies"] == {"minecraft": "1.20.1", "fabric-loader": "0.16.9"}
    files = {f["path"]: f for f in index["files"]}
    sodium = files["mods/AANobbMI-sod1.jar"]
    assert sodium["env"] == {"client": "required", "server": "unsupported"}
    assert sodium["hashes"]["sha1"] == sha1(JAR_SODIUM)
    assert sodium["downloads"] == ["https://cdn.modrinth.com/data/AANobbMI/sod1.jar"]


def test_curseforge_export_bundles_modrinth_jars_without_key(api):
    make_pack()
    client.post("/api/custom-packs/my-pack/mods", json={"source": "modrinth", "project_id": "sodium"})
    api.get("https://cdn.modrinth.com/data/AANobbMI/sod1.jar").respond(content=JAR_SODIUM)
    api.get("https://cdn.modrinth.com/data/P7dR8mSH/api1.jar").respond(content=JAR_API)
    r = client.get("/api/custom-packs/my-pack/export/curseforge")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert z.read("overrides/mods/AANobbMI-sod1.jar") == JAR_SODIUM
        assert "Sodium" in z.read("modlist.html").decode()
    assert manifest["minecraft"]["modLoaders"] == [{"id": "fabric-0.16.9", "primary": True}]
    assert manifest["files"] == []
    assert "bundled" in json.loads(r.headers["x-pack-warnings"])[0]


def test_curseforge_export_matches_fingerprints_with_key(api):
    settings_store.save({"curseforge_api_key": "test-key-1234"})
    make_pack()
    client.post("/api/custom-packs/my-pack/mods", json={"source": "modrinth", "project_id": "sodium"})
    api.get("https://cdn.modrinth.com/data/AANobbMI/sod1.jar").respond(content=JAR_SODIUM)
    api.get("https://cdn.modrinth.com/data/P7dR8mSH/api1.jar").respond(content=JAR_API)
    fp = pack_export.cf_fingerprint(JAR_SODIUM)
    api.post(f"{CF}/v1/fingerprints/432").respond(
        json={"data": {"exactMatches": [{"id": 394468, "file": {"id": 5000, "fileFingerprint": fp}}]}}
    )
    r = client.get("/api/custom-packs/my-pack/export/curseforge")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        manifest = json.loads(z.read("manifest.json"))
        names = z.namelist()
    assert manifest["files"] == [{"projectID": 394468, "fileID": 5000, "required": True}]
    assert "overrides/mods/P7dR8mSH-api1.jar" in names
    assert "overrides/mods/AANobbMI-sod1.jar" not in names


def _murmur2_reference(data: bytes, seed: int = 1) -> int:
    data = bytes(b for b in data if b not in (9, 10, 13, 32))
    m, n = 0x5BD1E995, len(data)
    h = (seed ^ n) & 0xFFFFFFFF
    i = 0
    while n - i >= 4:
        k = data[i] | data[i + 1] << 8 | data[i + 2] << 16 | data[i + 3] << 24
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> 24
        k = (k * m) & 0xFFFFFFFF
        h = ((h * m) & 0xFFFFFFFF) ^ k
        i += 4
    rest = data[i:]
    if len(rest) == 3:
        h ^= rest[2] << 16
    if len(rest) >= 2:
        h ^= rest[1] << 8
    if rest:
        h ^= rest[0]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    return h ^ (h >> 15)


@pytest.mark.parametrize("data", [b"", b"a", b"ab c", b"abc\n", b"hello world, minecraft\r\n\t!" * 7])
def test_cf_fingerprint(data):
    assert pack_export.cf_fingerprint(data) == _murmur2_reference(data)


def test_server_install_skips_client_mods_and_cleans_up(api, tmp_path):
    make_pack()
    client.post("/api/custom-packs/my-pack/mods", json={"source": "modrinth", "project_id": "sodium"})
    api.get("https://cdn.modrinth.com/data/P7dR8mSH/api1.jar").respond(content=JAR_API)
    server = tmp_path / "server"
    (server / "mods").mkdir(parents=True)
    (server / "mods" / "my-own-mod.jar").write_bytes(b"x")
    (server / "mods" / "old.jar").write_bytes(b"x")
    (server / pack_install.STATE_FILE).write_text(json.dumps({"files": ["old.jar"]}))

    notes = pack_install.install(pack_store.get("my-pack"), str(server))
    assert notes == ["Skipped Sodium: client-only."]
    assert sorted(p.name for p in (server / "mods").iterdir()) == ["P7dR8mSH-api1.jar", "my-own-mod.jar"]
    assert (server / "mods" / "P7dR8mSH-api1.jar").read_bytes() == JAR_API


def test_server_install_checksum_failure(api, tmp_path):
    make_pack()
    client.post("/api/custom-packs/my-pack/mods", json={"source": "modrinth", "project_id": "modmenu"})
    api.get("https://cdn.modrinth.com/data/P7dR8mSH/api1.jar").respond(content=b"tampered")
    api.get("https://cdn.modrinth.com/data/mOgUt4GM/menu1.jar").respond(content=JAR_MENU)
    with pytest.raises(RuntimeError, match="Fabric API"):
        pack_install.install(pack_store.get("my-pack"), str(tmp_path / "s"))


def test_custom_server_env(api):
    make_pack()
    env = build_env(CreateServerRequest(name="x", source="custom", project_id="my-pack"), settings)
    assert env["TYPE"] == "FABRIC"
    assert env["VERSION"] == "1.20.1"
    assert env["FABRIC_LOADER_VERSION"] == "0.16.9"


def test_neoforge_prefix():
    assert loader_versions.neoforge_prefix("1.21.1") == "21.1."
    assert loader_versions.neoforge_prefix("1.21") == "21.0."


def test_unknown_pack_404():
    assert client.get("/api/custom-packs/nope").status_code == 404
    assert client.get("/api/custom-packs/../etc").status_code == 404


def test_upstream_error_is_502(api):
    api.get(f"{MR}/search").mock(side_effect=httpx.ConnectError("down"))
    assert client.get("/api/mods/search").status_code == 502


def test_quilt_versions_sorted():
    with respx.mock() as mock:
        mock.get("https://meta.quiltmc.org/v3/versions/loader/1.20.1").respond(
            json=[{"loader": {"version": v}} for v in ["0.24.0", "0.23.0", "0.23.1", "0.25.0-beta.1"]]
        )
        import asyncio

        assert asyncio.run(loader_versions.loader_versions("quilt", "1.20.1")) == [
            "0.24.0",
            "0.23.1",
            "0.23.0",
            "0.25.0-beta.1",
        ]
