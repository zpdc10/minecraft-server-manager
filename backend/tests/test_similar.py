"""Tests for the similar-pack index: remote zip reading, mod extraction, scoring, API."""

import asyncio
import io
import json
import re
import zipfile

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app
from app.models import Modpack
from app.services import similar_index
from app.services.remote_zip import read_member
from app.services.similar_index import SimilarIndex, mod_ids_from_index

API = "https://api.modrinth.com/v2"
CDN = "https://cdn.modrinth.com/data"


def make_zip(files: dict[str, bytes], pad: int = 0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        if pad:
            # A big incompressible-ish overrides file, like real packs have.
            zf.writestr(zipfile.ZipInfo("overrides/big.bin"), bytes(range(256)) * (pad // 256))
    return buf.getvalue()


def range_responder(data: bytes, calls: list):
    def respond(request: httpx.Request):
        header = request.headers.get("Range", "")
        calls.append(header)
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
        if not m:
            return httpx.Response(200, content=data)
        start, end = m.groups()
        if start == "":
            chunk = data[-int(end) :]
        else:
            chunk = data[int(start) : (int(end) + 1 if end else None)]
        return httpx.Response(206, content=chunk)

    return respond


def mr_index(*project_ids: str, extra: list | None = None) -> bytes:
    files = [
        {
            "path": f"mods/{p}.jar",
            "downloads": [f"{CDN}/{p}/versions/VVVVVVVV/{p}.jar"],
        }
        for p in project_ids
    ]
    return json.dumps({"formatVersion": 1, "files": files + (extra or [])}).encode()


def test_mod_ids_from_index():
    index = json.loads(
        mr_index(
            "AAAAAAAA",
            "BBBBBBBB",
            extra=[
                {"path": "resourcepacks/x.zip", "downloads": [f"{CDN}/CCCCCCCC/versions/V/x.zip"]},
                {"path": "mods/github.jar", "downloads": ["https://github.com/x/y/releases/a.jar"]},
            ],
        )
    )
    assert mod_ids_from_index(index) == {"AAAAAAAA", "BBBBBBBB"}


@respx.mock
def test_read_member_uses_ranges_on_large_archive():
    data = make_zip({"modrinth.index.json": mr_index("AAAAAAAA")}, pad=400_000)
    calls: list = []
    respx.get("https://cdn.example/pack.mrpack").mock(side_effect=range_responder(data, calls))

    async def run():
        async with httpx.AsyncClient() as client:
            return await read_member(client, "https://cdn.example/pack.mrpack", "modrinth.index.json")

    raw = asyncio.run(run())
    assert json.loads(raw)["files"][0]["path"] == "mods/AAAAAAAA.jar"
    assert all(c.startswith("bytes=") for c in calls)
    assert len(calls) <= 4


@respx.mock
def test_read_member_falls_back_when_range_ignored():
    data = make_zip({"modrinth.index.json": b'{"files": []}', "other.txt": b"x"})
    respx.get("https://cdn.example/pack.mrpack").mock(return_value=httpx.Response(200, content=data))

    async def run():
        async with httpx.AsyncClient() as client:
            return (
                await read_member(client, "https://cdn.example/pack.mrpack", "modrinth.index.json"),
                await read_member(client, "https://cdn.example/pack.mrpack", "missing.json"),
            )

    found, missing = asyncio.run(run())
    assert found == b'{"files": []}'
    assert missing is None


def pack(pid: str, versions=("1.20.1",), loaders=("fabric",)) -> Modpack:
    return Modpack(
        source="modrinth",
        id=pid,
        slug=pid.lower(),
        title=pid,
        description="",
        author="a",
        downloads=1,
        follows=1,
        loaders=list(loaders),
        categories=[],
        game_versions=list(versions),
        url=f"https://modrinth.com/modpack/{pid}",
    )


def test_similarity_weights_rare_mods(tmp_path):
    idx = SimilarIndex(tmp_path / "idx.sqlite3")
    libs = {f"LIB{i}" for i in range(5)}  # in every pack, so they carry little weight
    idx._store_pack(pack("TARGET"), "v", libs | {"CREATE", "CREATEADDON"})
    idx._store_pack(pack("CREATEPACK"), "v", {"CREATE", "CREATEADDON", "LIB0"})
    idx._store_pack(pack("LIBPACK"), "v", libs | {"OTHER"})
    idx._store_pack(pack("FORGEPACK", loaders=("forge",)), "v", libs | {"CREATE"})
    idx._store_pack(pack("OLDPACK", versions=("1.12.2",)), "v", libs | {"CREATE"})
    idx._store_pack(pack("UNRELATED"), "v", {"NOPE"})

    by_score = idx.similar("TARGET")
    ids = [r["pack"].id for r in by_score["results"]]
    assert "UNRELATED" not in ids
    # Sharing the two rare Create mods beats sharing all five common libraries.
    assert ids.index("CREATEPACK") < ids.index("LIBPACK")
    create = next(r for r in by_score["results"] if r["pack"].id == "CREATEPACK")
    # Shared mods are listed rarest first.
    assert create["shared_mod_ids"] == ["CREATEADDON", "CREATE", "LIB0"]

    by_shared = idx.similar("TARGET", sort="shared")
    assert by_shared["results"][-1]["pack"].id == "CREATEPACK"

    fabric = [r["pack"].id for r in idx.similar("TARGET", loader="fabric")["results"]]
    assert "FORGEPACK" not in fabric
    modern = [r["pack"].id for r in idx.similar("TARGET", game_version="1.20.1")["results"]]
    assert "OLDPACK" not in modern
    assert idx.similar("MISSING")["total"] == 0


def search_hit(pid: str, version_id: str) -> dict:
    return {
        "project_id": pid,
        "slug": pid.lower(),
        "title": pid,
        "description": "",
        "author": "a",
        "downloads": 10,
        "follows": 1,
        "categories": ["fabric"],
        "versions": ["1.20.1"],
        "latest_version": version_id,
    }


@pytest.fixture
def isolated_index(tmp_path, monkeypatch):
    idx = SimilarIndex(tmp_path / "idx.sqlite3")
    monkeypatch.setattr(similar_index, "_index", idx)
    monkeypatch.setattr(similar_index, "API_DELAY", 0)
    return idx


@respx.mock
def test_similar_endpoint_indexes_and_ranks(isolated_index):
    packs = {
        "PACKAAAA": ("VERAAAAA", ["MODAAAAA", "MODBBBBB", "MODCCCCC"]),
        "PACKBBBB": ("VERBBBBB", ["MODAAAAA", "MODBBBBB"]),
        "PACKCCCC": ("VERCCCCC", ["MODDDDDD"]),
    }
    hits = [search_hit(p, v) for p, (v, _) in packs.items()]

    def search(request: httpx.Request):
        facets = request.url.params.get("facets", "")
        m = re.search(r"project_id:(\w+)", facets)
        found = [h for h in hits if h["project_id"] == m.group(1)] if m else hits
        return httpx.Response(200, json={"hits": found, "total_hits": len(found)})

    respx.get(f"{API}/search").mock(side_effect=search)
    for pid, (vid, mods) in packs.items():
        url = f"https://cdn.modrinth.com/data/{pid}/versions/{vid}/pack.mrpack"
        respx.get(f"{API}/version/{vid}").mock(
            return_value=httpx.Response(
                200, json={"id": vid, "dependencies": [], "files": [{"url": url, "primary": True}]}
            )
        )
        data = make_zip({"modrinth.index.json": mr_index(*mods)})
        respx.get(url).mock(side_effect=range_responder(data, []))
    respx.get(f"{API}/projects").mock(
        side_effect=lambda req: httpx.Response(
            200, json=[{"id": i, "title": f"Mod {i}", "slug": i.lower()} for i in json.loads(req.url.params["ids"])]
        )
    )

    with TestClient(app) as client:
        resp = client.get("/api/similar/modrinth/PACKAAAA")
        assert resp.status_code == 200, resp.text
        # Let the background crawl that the first request started finish.
        task = isolated_index._task
        assert task is not None
        client.portal.call(lambda: task)

        body = client.get("/api/similar/modrinth/PACKAAAA").json()
    assert body["pack"]["id"] == "PACKAAAA"
    assert body["mod_count"] == 3
    assert body["index"]["state"] == "idle"
    assert body["index"]["indexed"] == 3
    assert [r["pack"]["id"] for r in body["results"]] == ["PACKBBBB"]
    shared = body["results"][0]
    assert shared["shared_count"] == 2
    assert {m["title"] for m in shared["shared_mods"]} == {"Mod MODAAAAA", "Mod MODBBBBB"}


def test_similar_rejects_curseforge():
    resp = TestClient(app).get("/api/similar/curseforge/123")
    assert resp.status_code == 400
