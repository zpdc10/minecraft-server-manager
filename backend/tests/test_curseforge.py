import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import settings_store
from app.main import app
from app.providers import curseforge

client = TestClient(app)
API = "https://api.curseforge.com"

MOD = {
    "id": 925200,
    "name": "All the Mods 10",
    "slug": "all-the-mods-10",
    "summary": "Kitchen sink pack",
    "downloadCount": 1234,
    "thumbsUpCount": 56,
    "authors": [{"name": "ATMTeam"}],
    "logo": {"thumbnailUrl": "https://media.forgecdn.net/thumb.png", "url": "https://media.forgecdn.net/full.png"},
    "links": {"websiteUrl": "https://www.curseforge.com/minecraft/modpacks/all-the-mods-10"},
    "latestFilesIndexes": [
        {"gameVersion": "1.21.1", "fileId": 3, "modLoader": 6},
        {"gameVersion": "1.21.1", "fileId": 2, "modLoader": 6},
        {"gameVersion": "1.20.1", "fileId": 1, "modLoader": 1},
    ],
    "categories": [{"name": "Tech"}, {"name": "Magic"}],
    "dateModified": "2026-10-01T00:00:00Z",
}


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_store, "_path", lambda: path)
    curseforge._cache.clear()
    return path


@pytest.fixture
def key(store):
    settings_store.save({"curseforge_api_key": "test-key-1234"})


def test_sources_include_curseforge():
    assert "curseforge" in client.get("/api/sources").json()


def test_search_without_key_explains_how_to_get_one():
    resp = client.get("/api/search", params={"source": "curseforge"})
    assert resp.status_code == 400
    assert "console.curseforge.com" in resp.json()["detail"]


@respx.mock
def test_search_maps_filters_and_normalizes(key):
    respx.get(f"{API}/v1/categories").mock(
        return_value=httpx.Response(200, json={"data": [{"id": 4472, "name": "Tech", "classId": 4471}]})
    )
    route = respx.get(f"{API}/v1/mods/search").mock(
        return_value=httpx.Response(200, json={"data": [MOD], "pagination": {"index": 0, "totalCount": 1}})
    )
    resp = client.get(
        "/api/search",
        params={
            "source": "curseforge",
            "q": "atm",
            "game_version": "1.21.1",
            "loader": "neoforge",
            "category": "Tech",
            "sort": "downloads",
        },
    )
    assert resp.status_code == 200
    sent = route.calls.last.request
    assert sent.headers["x-api-key"] == "test-key-1234"
    assert sent.headers["User-Agent"].startswith("zpdc10/minecraft-server-manager")
    q = dict(sent.url.params)
    assert q["gameId"] == "432" and q["classId"] == "4471"
    assert q["searchFilter"] == "atm" and q["gameVersion"] == "1.21.1"
    assert q["modLoaderType"] == "6" and q["categoryId"] == "4472" and q["sortField"] == "6"
    pack = resp.json()["hits"][0]
    assert pack["source"] == "curseforge" and pack["id"] == "925200" and pack["slug"] == "all-the-mods-10"
    assert pack["game_versions"] == ["1.21.1", "1.20.1"]
    assert pack["loaders"] == ["forge", "neoforge"]
    assert pack["follows"] == 56
    assert pack["icon_url"] == "https://media.forgecdn.net/thumb.png"


@respx.mock
def test_search_omits_empty_params_and_clamps_window(key):
    route = respx.get(f"{API}/v1/mods/search").mock(
        return_value=httpx.Response(200, json={"data": [], "pagination": {"totalCount": 50000}})
    )
    body = client.get("/api/search", params={"source": "curseforge", "offset": 99990, "limit": 100}).json()
    q = dict(route.calls.last.request.url.params)
    assert "searchFilter" not in q and "modLoaderType" not in q and "gameVersion" not in q
    assert q["pageSize"] == "50" and int(q["index"]) + 50 <= 10000
    assert body["total"] == 10000


@respx.mock
def test_rejected_key_is_400(key):
    respx.get(f"{API}/v1/mods/search").mock(return_value=httpx.Response(403))
    resp = client.get("/api/search", params={"source": "curseforge"})
    assert resp.status_code == 400
    assert "rejected" in resp.json()["detail"]


@respx.mock
def test_upstream_error_is_502(key):
    respx.get(f"{API}/v1/mods/search").mock(return_value=httpx.Response(500))
    assert client.get("/api/search", params={"source": "curseforge"}).status_code == 502


@respx.mock
def test_filters(key):
    respx.get(f"{API}/v1/minecraft/version").mock(
        return_value=httpx.Response(
            200, json={"data": [{"versionString": "1.21.1"}, {"versionString": "24w10a"}, {"versionString": "1.20"}]}
        )
    )
    respx.get(f"{API}/v1/categories").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": 1, "name": "Tech", "classId": 4471},
                    {"id": 2, "name": "Adventure", "classId": 4471},
                    {"id": 3, "name": "Mods class", "classId": 6},
                ]
            },
        )
    )
    body = client.get("/api/filters", params={"source": "curseforge"}).json()
    assert body["game_versions"] == ["1.21.1", "1.20"]
    assert body["loaders"] == ["fabric", "forge", "neoforge", "quilt"]
    assert body["categories"] == ["Adventure", "Tech"]


@respx.mock
def test_versions_paginate_and_skip_server_packs(key):
    page1 = [
        {
            "id": i,
            "displayName": f"ATM10 {i}",
            "fileName": f"atm10-{i}.zip",
            "gameVersions": ["1.21.1", "NeoForge"],
            "fileDate": f"2026-01-{i % 28 + 1:02d}T00:00:00Z",
        }
        for i in range(1, 51)
    ]
    page2 = [
        {"id": 99, "displayName": "Server", "isServerPack": True, "gameVersions": [], "fileDate": "2026-09-01"},
        {
            "id": 100,
            "displayName": "ATM10 5.0",
            "fileName": "atm10-5.0.zip",
            "gameVersions": ["1.21.1", "NeoForge", "Server"],
            "fileDate": "2026-08-01T00:00:00Z",
        },
    ]
    respx.get(f"{API}/v1/mods/925200/files").mock(
        side_effect=[
            httpx.Response(200, json={"data": page1, "pagination": {"totalCount": 52}}),
            httpx.Response(200, json={"data": page2, "pagination": {"totalCount": 52}}),
        ]
    )
    body = client.get("/api/packs/curseforge/925200/versions").json()
    assert len(body) == 51
    assert body[0] == {
        "id": "100",
        "name": "ATM10 5.0",
        "version_number": "atm10-5.0.zip",
        "game_versions": ["1.21.1"],
        "loaders": ["neoforge"],
        "published": "2026-08-01T00:00:00Z",
    }


def test_versions_rejects_non_numeric_id(key):
    assert client.get("/api/packs/curseforge/abc/versions").status_code == 400


# Settings


def test_settings_empty():
    assert client.get("/api/settings").json()["curseforge"] == {
        "configured": False,
        "key_source": None,
        "key_hint": None,
        "verified": None,
    }


def test_env_key_is_fallback(monkeypatch):
    monkeypatch.setattr(settings_store, "settings", type("S", (), {"curseforge_api_key": "env-key-5678"})())
    assert settings_store.curseforge_api_key() == "env-key-5678"
    assert client.get("/api/settings").json()["curseforge"]["key_source"] == "env"
    settings_store.save({"curseforge_api_key": "ui-key-0000"})
    assert settings_store.curseforge_api_key() == "ui-key-0000"


@respx.mock
def test_save_valid_key(store):
    respx.get(f"{API}/v1/games/432").mock(return_value=httpx.Response(200, json={"data": {}}))
    resp = client.put("/api/settings/curseforge", json={"api_key": "  $2a$10$abcdefgh  "})
    assert resp.status_code == 200
    assert resp.json() == {"configured": True, "key_source": "settings", "key_hint": "…efgh", "verified": True}
    assert json.loads(store.read_text())["curseforge_api_key"] == "$2a$10$abcdefgh"
    assert "abcdefgh" not in client.get("/api/settings").text.replace("…efgh", "")


@respx.mock
def test_invalid_key_not_saved(store):
    respx.get(f"{API}/v1/games/432").mock(return_value=httpx.Response(403))
    assert client.put("/api/settings/curseforge", json={"api_key": "bad-key-xxxx"}).status_code == 400
    assert not store.exists()


@respx.mock
def test_unreachable_key_saved_unverified(store):
    respx.get(f"{API}/v1/games/432").mock(side_effect=httpx.ConnectError("down"))
    resp = client.put("/api/settings/curseforge", json={"api_key": "maybe-key-1234"})
    assert resp.status_code == 200 and resp.json()["verified"] is None


def test_clear_key(key, store):
    resp = client.put("/api/settings/curseforge", json={"api_key": ""})
    assert resp.json()["configured"] is False
    assert "curseforge_api_key" not in json.loads(store.read_text())
