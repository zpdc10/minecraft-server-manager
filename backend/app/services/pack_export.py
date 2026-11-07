"""Export a custom pack in the two formats launchers import.

- Modrinth .mrpack (https://support.modrinth.com/en/articles/8802351-modrinth-modpack-format-mrpack):
  imported by the Modrinth App, Prism, GDLauncher, ATLauncher and others.
- CurseForge modpack zip (manifest.json + overrides/): imported by the CurseForge
  app, GDLauncher, Prism and others.

Each format can only reference files from its own platform (the Modrinth format
also allows GitHub/GitLab links). A mod from the other platform is matched to the
same file on this platform by hash when possible, otherwise its jar is bundled in
the pack's overrides folder.
"""

import hashlib
import io
import json
import struct
import zipfile
from dataclasses import dataclass, field
from html import escape
from urllib.parse import urlparse

import httpx

from .. import settings_store
from ..config import USER_AGENT
from ..models.pack import CustomPack, PackMod
from ..providers import curseforge, modrinth

MRPACK_ALLOWED_HOSTS = {"cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com"}
MRPACK_LOADER_KEYS = {"fabric": "fabric-loader", "quilt": "quilt-loader", "forge": "forge", "neoforge": "neoforge"}
MAX_JAR_BYTES = 256 * 1024 * 1024


@dataclass
class ExportResult:
    filename: str
    data: bytes
    warnings: list[str] = field(default_factory=list)


class ExportError(Exception):
    pass


def safe_filename(name: str) -> str:
    keep = "".join(c if (c.isascii() and c.isalnum()) or c in "-_. " else "_" for c in name).strip()
    return keep or "pack"


def _jar_name(m: PackMod) -> str:
    name = m.file_name.replace("/", "_").replace("\\", "_")
    return name if name.endswith(".jar") else f"{name or m.slug or m.project_id}.jar"


def _env_value(side: str) -> str:
    return side if side in ("required", "optional", "unsupported") else "required"


async def _download(client: httpx.AsyncClient, m: PackMod) -> bytes:
    if not m.download_url:
        raise ExportError(f"{m.title} has no download link")
    async with client.stream("GET", m.download_url) as resp:
        resp.raise_for_status()
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_JAR_BYTES:
                raise ExportError(f"{m.title} is too large to bundle")
            chunks.append(chunk)
    data = b"".join(chunks)
    if m.sha1 and hashlib.sha1(data).hexdigest() != m.sha1.lower():
        raise ExportError(f"{m.title} failed its checksum")
    return data


def cf_fingerprint(data: bytes) -> int:
    """CurseForge's file fingerprint: MurmurHash2 (seed 1) over the file minus whitespace bytes."""
    data = data.translate(None, b"\t\n\r ")
    m, mask = 0x5BD1E995, 0xFFFFFFFF
    length = len(data)
    h = (1 ^ length) & mask
    aligned = length - (length & 3)
    for (k,) in struct.iter_unpack("<I", data[:aligned]):
        k = (k * m) & mask
        k ^= k >> 24
        k = (k * m) & mask
        h = ((h * m) & mask) ^ k
    tail = data[aligned:]
    if len(tail) == 3:
        h ^= tail[2] << 16
    if len(tail) >= 2:
        h ^= tail[1] << 8
    if len(tail) >= 1:
        h ^= tail[0]
        h = (h * m) & mask
    h ^= h >> 13
    h = (h * m) & mask
    h ^= h >> 15
    return h


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True)


async def _modrinth_files_by_sha1(sha1s: list[str]) -> dict[str, dict]:
    """sha1 -> Modrinth file dict (url, hashes, size, filename) for files Modrinth also hosts."""
    if not sha1s:
        return {}
    async with modrinth._client() as client:
        resp = await client.post("/version_files", json={"hashes": sha1s, "algorithm": "sha1"})
    if resp.status_code != 200:
        return {}
    out = {}
    for sha1, version in resp.json().items():
        for f in version.get("files") or []:
            if (f.get("hashes") or {}).get("sha1") == sha1:
                out[sha1] = f
    return out


async def export_mrpack(pack: CustomPack) -> ExportResult:
    warnings: list[str] = []
    files: list[dict] = []
    bundled: dict[str, bytes] = {}

    cf_sha1s = [m.sha1 for m in pack.mods if m.source == "curseforge" and m.sha1]
    try:
        on_modrinth = await _modrinth_files_by_sha1(cf_sha1s)
    except httpx.HTTPError:
        on_modrinth = {}

    async with _http() as client:
        for m in pack.mods:
            env = {"client": _env_value(m.client_side), "server": _env_value(m.server_side)}
            url, sha512, size = m.download_url, m.sha512, m.size
            mr_file = on_modrinth.get(m.sha1) if m.source == "curseforge" else None
            if mr_file:
                url, sha512, size = mr_file["url"], mr_file["hashes"].get("sha512", ""), mr_file.get("size", size)
            host = urlparse(url or "").hostname or ""
            if url and host in MRPACK_ALLOWED_HOSTS and m.sha1 and sha512:
                files.append(
                    {
                        "path": f"mods/{_jar_name(m)}",
                        "hashes": {"sha1": m.sha1, "sha512": sha512},
                        "env": env,
                        "downloads": [url],
                        "fileSize": size,
                    }
                )
                continue
            try:
                bundled[_jar_name(m)] = await _download(client, m)
                warnings.append(f"{m.title} isn't on Modrinth, so its jar is bundled inside the pack.")
            except (ExportError, httpx.HTTPError) as e:
                warnings.append(f"Left out {m.title}: {e}. Add it by hand in your launcher.")

    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": pack.version,
        "name": pack.name,
        "summary": pack.summary,
        "files": files,
        "dependencies": {"minecraft": pack.minecraft_version, MRPACK_LOADER_KEYS[pack.loader]: pack.loader_version},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("modrinth.index.json", json.dumps(index, indent=2))
        for name, data in bundled.items():
            z.writestr(f"overrides/mods/{name}", data)
    return ExportResult(f"{safe_filename(pack.name)}-{safe_filename(pack.version)}.mrpack", buf.getvalue(), warnings)


async def _cf_matches(fingerprints: list[int]) -> dict[int, tuple[int, int]]:
    """fingerprint -> (projectID, fileID) for jars CurseForge hosts. Needs an API key."""
    if not fingerprints or not settings_store.curseforge_api_key():
        return {}
    async with curseforge._client(settings_store.curseforge_api_key()) as client:
        resp = await client.post(
            f"/v1/fingerprints/{curseforge.MINECRAFT_GAME_ID}", json={"fingerprints": fingerprints}
        )
    if resp.status_code != 200:
        return {}
    out = {}
    for match in (resp.json().get("data") or {}).get("exactMatches") or []:
        f = match.get("file") or {}
        if f.get("fileFingerprint") is not None:
            out[int(f["fileFingerprint"])] = (int(match["id"]), int(f["id"]))
    return out


async def export_curseforge(pack: CustomPack) -> ExportResult:
    warnings: list[str] = []
    entries: list[dict] = []
    listed: list[PackMod] = []
    jars: dict[str, tuple[PackMod, bytes]] = {}

    async with _http() as client:
        for m in pack.mods:
            if m.source == "curseforge":
                entries.append({"projectID": int(m.project_id), "fileID": int(m.version_id), "required": True})
                listed.append(m)
                continue
            try:
                jars[_jar_name(m)] = (m, await _download(client, m))
            except (ExportError, httpx.HTTPError) as e:
                warnings.append(f"Left out {m.title}: {e}. Add it by hand in your launcher.")

    prints = {name: cf_fingerprint(data) for name, (_, data) in jars.items()}
    try:
        matches = await _cf_matches(list(prints.values()))
    except httpx.HTTPError:
        matches = {}
    bundled: dict[str, bytes] = {}
    for name, (m, data) in jars.items():
        hit = matches.get(prints[name])
        if hit:
            entries.append({"projectID": hit[0], "fileID": hit[1], "required": True})
            listed.append(m)
        else:
            bundled[name] = data
    if bundled:
        why = "" if settings_store.curseforge_api_key() else " (add a CurseForge API key to match them)"
        warnings.append(
            f"{len(bundled)} Modrinth mod(s) weren't matched to CurseForge files{why}, so their jars are bundled "
            "inside the pack."
        )

    loader_id = f"{pack.loader}-{pack.loader_version}"
    manifest = {
        "minecraft": {"version": pack.minecraft_version, "modLoaders": [{"id": loader_id, "primary": True}]},
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": pack.name,
        "version": pack.version,
        "author": pack.author,
        "files": entries,
        "overrides": "overrides",
    }
    in_pack = listed + [jars[name][0] for name in bundled]
    items = "".join(f'<li><a href="{escape(m.url)}">{escape(m.title)}</a></li>' for m in in_pack)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("modlist.html", f"<ul>{items}</ul>\n")
        for name, data in bundled.items():
            z.writestr(f"overrides/mods/{name}", data)
    filename = f"{safe_filename(pack.name)}-{safe_filename(pack.version)}-curseforge.zip"
    return ExportResult(filename, buf.getvalue(), warnings)
