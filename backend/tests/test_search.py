import json

import httpx
import respx
from fastapi.testclient import TestClient

from app.main import app
from app.providers import modrinth
from app.providers.modrinth import build_facets

client = TestClient(app)
API = "https://api.modrinth.com/v2"

HIT = {
    "project_id": "abc123",
    "slug": "cool-pack",
    "title": "Cool Pack",
    "description": "A pack",
    "author": "someone",
    "icon_url": "",
    "downloads": 1000,
    "follows": 50,
    "categories": ["fabric", "adventure"],
    "display_categories": ["fabric", "adventure", "quests"],
    "versions": ["1.20.1"],
    "date_modified": "2026-01-01T00:00:00Z",
}


def test_build_facets():
    facets = json.loads(build_facets("1.20.1", "fabric", "adventure"))
    assert ["project_type:modpack"] in facets
    assert ["server_side!=unsupported"] in facets
    assert ["versions:1.20.1"] in facets
    assert ["categories:fabric"] in facets
    assert ["categories:adventure"] in facets


@respx.mock
def test_search_normalizes_hits():
    route = respx.get(f"{API}/search").mock(
        return_value=httpx.Response(200, json={"hits": [HIT], "offset": 0, "limit": 20, "total_hits": 1})
    )
    resp = client.get("/api/search", params={"q": "cool", "loader": "fabric", "game_version": "1.20.1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    pack = body["hits"][0]
    assert pack["source"] == "modrinth"
    assert pack["loaders"] == ["fabric"]
    assert pack["categories"] == ["adventure", "quests"]
    assert pack["icon_url"] is None
    assert pack["url"] == "https://modrinth.com/modpack/cool-pack"
    sent = route.calls.last.request
    assert sent.headers["User-Agent"].startswith("zpdc10/minecraft-server-manager")
    assert "versions%3A1.20.1" in str(sent.url)


@respx.mock
def test_upstream_error_returns_502():
    respx.get(f"{API}/search").mock(return_value=httpx.Response(500))
    assert client.get("/api/search").status_code == 502


def test_unknown_source_rejected():
    assert client.get("/api/search", params={"source": "nope"}).status_code == 422


@respx.mock
def test_filters():
    modrinth._cache.clear()
    respx.get(f"{API}/tag/game_version").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"version": "1.21.1", "version_type": "release"},
                {"version": "24w10a", "version_type": "snapshot"},
                {"version": "1.20.1", "version_type": "release"},
            ],
        )
    )
    respx.get(f"{API}/tag/loader").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "fabric", "supported_project_types": ["mod", "modpack"]},
                {"name": "paper", "supported_project_types": ["plugin"]},
                {"name": "forge", "supported_project_types": ["mod", "modpack"]},
            ],
        )
    )
    respx.get(f"{API}/tag/category").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "adventure", "project_type": "modpack", "header": "categories"},
                {"name": "adventure", "project_type": "mod", "header": "categories"},
                {"name": "lightweight", "project_type": "modpack", "header": "categories"},
            ],
        )
    )
    body = client.get("/api/filters").json()
    assert body["game_versions"] == ["1.21.1", "1.20.1"]
    assert body["loaders"] == ["fabric", "forge"]
    assert body["categories"] == ["adventure", "lightweight"]


@respx.mock
def test_versions():
    respx.get(f"{API}/project/abc123/version").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "v1",
                    "name": "Cool Pack 1.0",
                    "version_number": "1.0",
                    "game_versions": ["1.20.1"],
                    "loaders": ["fabric"],
                    "date_published": "2026-01-01",
                }
            ],
        )
    )
    body = client.get("/api/packs/modrinth/abc123/versions").json()
    assert body[0]["id"] == "v1"
    assert body[0]["loaders"] == ["fabric"]
