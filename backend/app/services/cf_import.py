"""Import a CurseForge modpack from a link or an uploaded zip, without an API key.

CurseForge's official API needs a key. Without one, this uses only public,
unauthenticated sources, and never tries to get past CurseForge's Cloudflare checks:

- cfwidget.com (a public CurseForge mirror) turns a link or slug into the project id,
  title and file list.
- www.curseforge.com/api/v1/mods/<project>/files/<file> (the JSON the website's own
  download pages use) gives a file's name, whether it has a server pack, and which
  sides (Client / Server) a mod file is tagged for.
- mediafilez.forgecdn.net, CurseForge's public download CDN, serves the files.

If any of those stop working, the user can download the pack zip in their browser
and upload it instead; the mods inside it are then fetched the same way.

Install (runs in the server provisioning thread, before itzg starts):
1. If the pack file has a server pack, download it and unpack it into /data.
2. Otherwise download the client zip, unpack its overrides into /data and download
   every mod in manifest.json into /data/mods. Mods tagged client-only on CurseForge
   are skipped; a mod CurseForge can't give us is looked up on Modrinth by slug.
3. Tell itzg which loader to install (TYPE=FORGE / FABRIC / ...), from the manifest.

The server builder then test-starts the server and removes mods that break it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx

from ..config import USER_AGENT, settings
from ..models.cf_import import ImportFile, ImportPreview
from ..providers import ProviderError

log = logging.getLogger(__name__)

CFWIDGET = "https://api.cfwidget.com"
CF_SITE_API = "https://www.curseforge.com/api/v1"
CF_CDN = "https://mediafilez.forgecdn.net/files"
MODRINTH = "https://api.modrinth.com/v2"
DOWNLOAD_WORKERS = 6
MAX_PACK_BYTES = 4 * 1024**3  # server packs can be over 1 GB
UPLOAD_PREFIX = "upload-"
LOADERS = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric", "quilt": "Quilt"}
# itzg TYPE and the variable that pins the loader version.
LOADER_ENV = {
    "fabric": ("FABRIC", "FABRIC_LOADER_VERSION"),
    "quilt": ("QUILT", "QUILT_LOADER_VERSION"),
    "forge": ("FORGE", "FORGE_VERSION"),
    "neoforge": ("NEOFORGE", "NEOFORGE_VERSION"),
}

_LINK_RE = re.compile(
    r"curseforge\.com/minecraft/modpacks/(?P<slug>[a-z0-9][a-z0-9_-]*)"
    r"(?:/(?:files|download)/(?P<file>\d+))?",
    re.IGNORECASE,
)
_PROJECT_ID_RE = re.compile(r"curseforge\.com/projects/(?P<id>\d+)", re.IGNORECASE)


class PackImportError(RuntimeError):
    """The pack can't be imported. The message is shown to the user."""


# ---- links and project lookups -------------------------------------------------------


@dataclass
class Link:
    slug: str = ""
    project_id: str = ""
    file_id: str = ""


def parse_link(text: str) -> Link:
    """Read a CurseForge modpack page link, a project id or a bare slug."""
    text = (text or "").strip()
    if m := _LINK_RE.search(text):
        return Link(slug=m["slug"].lower(), file_id=m["file"] or "")
    if m := _PROJECT_ID_RE.search(text):
        return Link(project_id=m["id"])
    if text.isdigit():
        return Link(project_id=text)
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", text, re.IGNORECASE):
        return Link(slug=text.lower())
    raise ProviderError(
        "That doesn't look like a CurseForge modpack link. Paste a link like "
        "https://www.curseforge.com/minecraft/modpacks/all-the-mods-9",
        status_code=400,
    )


def cdn_url(file_id: int | str, file_name: str) -> str:
    fid = int(file_id)
    return f"{CF_CDN}/{fid // 1000}/{fid % 1000}/{quote(file_name)}"


def _loader_of(versions: list[str]) -> str:
    lowered = {v.lower() for v in versions}
    for key in ("neoforge", "forge", "fabric", "quilt"):  # neoforge before forge
        if key in lowered:
            return key
    return ""


def _mc_of(versions: list[str]) -> str:
    mcs = [v for v in versions if re.fullmatch(r"\d+\.\d+(\.\d+)?", v)]
    return mcs[0] if mcs else ""


def _client(**kw) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True, **kw)


def _async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True)


async def preview(text: str) -> ImportPreview:
    """Look up the pack behind a link: its title and files, newest first."""
    link = parse_link(text)
    path = f"/{link.project_id}" if link.project_id else f"/minecraft/modpacks/{link.slug}"
    async with _async_client() as client:
        try:
            resp = await client.get(CFWIDGET + path)
        except httpx.HTTPError as e:
            raise ProviderError(f"Couldn't reach cfwidget.com to look up the pack: {e}", status_code=502) from e
        if resp.status_code == 202:
            raise ProviderError(
                "cfwidget.com is fetching this pack for the first time. Try again in a minute.", status_code=503
            )
        if resp.status_code == 404:
            raise ProviderError("No CurseForge modpack was found at that link.", status_code=404)
        if resp.status_code != 200:
            raise ProviderError(f"cfwidget.com answered {resp.status_code} for that pack.", status_code=502)
        data = resp.json()
        if (data.get("type") or "").lower() not in ("modpacks", "modpack", ""):
            raise ProviderError("That link is a CurseForge mod, not a modpack.", status_code=400)

        files = [
            ImportFile(
                id=str(f["id"]),
                name=f.get("name", ""),
                display_name=f.get("display") or f.get("name", ""),
                minecraft_version=f.get("version") or _mc_of(f.get("versions", [])),
                loader=_loader_of(f.get("versions", [])),
                release_type=f.get("type", "release"),
                uploaded_at=f.get("uploaded_at", ""),
                size=int(f.get("filesize") or 0),
            )
            for f in data.get("files", [])
        ]
        files.sort(key=lambda f: int(f.id), reverse=True)
        selected = link.file_id or (next((f.id for f in files if f.release_type == "release"), "") or "")
        if not selected and files:
            selected = files[0].id

        has_server_pack = None
        if selected:
            info = await _site_file(client, str(data["id"]), selected)
            if info is not None:
                has_server_pack = bool(info.get("hasServerPack"))

    urls = data.get("urls") or {}
    thumb = data.get("thumbnail") or ""
    return ImportPreview(
        project_id=str(data["id"]),
        slug=link.slug or (urls.get("curseforge", "").rstrip("/").rsplit("/", 1)[-1]),
        title=data.get("title", ""),
        summary=data.get("summary", ""),
        icon_url=thumb or None,
        url=urls.get("curseforge", ""),
        files=files,
        selected_file_id=selected,
        selected_has_server_pack=has_server_pack,
    )


async def has_server_pack(project_id: str, file_id: str) -> bool | None:
    async with _async_client() as client:
        info = await _site_file(client, project_id, file_id)
    return None if info is None else bool(info.get("hasServerPack"))


async def _site_file(client: httpx.AsyncClient, project_id: str, file_id: str) -> dict | None:
    try:
        resp = await client.get(f"{CF_SITE_API}/mods/{project_id}/files/{file_id}")
        if resp.status_code == 200:
            return resp.json().get("data")
    except (httpx.HTTPError, ValueError):
        pass
    return None


# ---- uploads -------------------------------------------------------------------------


def imports_dir() -> Path:
    path = Path(settings.data_dir) / "imports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_manifest(zip_path: str | Path) -> dict:
    try:
        with zipfile.ZipFile(zip_path) as z:
            if "manifest.json" not in z.namelist():
                raise PackImportError(
                    "This zip has no manifest.json. Upload the pack file CurseForge's Download button gives you "
                    "(not a server pack)."
                )
            return json.loads(z.read("manifest.json"))
    except zipfile.BadZipFile as e:
        raise PackImportError("That file isn't a zip.") from e
    except ValueError as e:
        raise PackImportError("The pack's manifest.json is broken.") from e


def new_upload() -> Path:
    """A fresh path for an uploaded pack zip. Its stem is the upload id."""
    return imports_dir() / f"{UPLOAD_PREFIX}{uuid.uuid4().hex[:12]}.zip"


def describe_upload(upload_id: str, filename: str = "") -> ImportPreview:
    manifest = read_manifest(upload_path(upload_id))
    pack = parse_manifest(manifest)
    title = manifest.get("name") or Path(filename).stem or "Uploaded pack"
    file = ImportFile(
        id=upload_id,
        name=filename,
        display_name=f"{title} {manifest.get('version', '')}".strip(),
        minecraft_version=pack.minecraft_version,
        loader=pack.loader,
        mod_count=len(pack.files),
    )
    return ImportPreview(
        project_id=upload_id,
        slug="",
        title=title,
        summary=f"by {manifest['author']}" if manifest.get("author") else "",
        files=[file],
        selected_file_id=upload_id,
        selected_has_server_pack=False,
    )


def upload_path(upload_id: str) -> Path:
    if not re.fullmatch(UPLOAD_PREFIX + r"[0-9a-f]{12}", upload_id):
        raise PackImportError("Unknown upload.")
    path = imports_dir() / f"{upload_id}.zip"
    if not path.exists():
        raise PackImportError("The uploaded pack is gone. Upload it again.")
    return path


# ---- manifest ------------------------------------------------------------------------


@dataclass
class PackSpec:
    minecraft_version: str
    loader: str
    loader_version: str
    files: list[tuple[int, int]] = field(default_factory=list)  # (projectID, fileID), required only
    overrides: str = "overrides"


def parse_manifest(manifest: dict) -> PackSpec:
    mc = manifest.get("minecraft") or {}
    loaders = mc.get("modLoaders") or []
    primary = next((lo for lo in loaders if lo.get("primary")), loaders[0] if loaders else None)
    if not primary or "-" not in primary.get("id", ""):
        raise PackImportError("The pack's manifest doesn't say which mod loader it uses.")
    loader, _, version = primary["id"].partition("-")
    loader = loader.lower()
    if loader not in LOADER_ENV:
        raise PackImportError(f"This pack uses {loader}, which isn't supported.")
    if not mc.get("version"):
        raise PackImportError("The pack's manifest doesn't say which Minecraft version it uses.")
    files = [
        (int(f["projectID"]), int(f["fileID"]))
        for f in manifest.get("files", [])
        if f.get("required", True) and "projectID" in f and "fileID" in f
    ]
    return PackSpec(mc["version"], loader, version, files, manifest.get("overrides") or "overrides")


def loader_env(spec: PackSpec) -> dict[str, str]:
    server_type, version_var = LOADER_ENV[spec.loader]
    env = {"TYPE": server_type, "VERSION": spec.minecraft_version}
    if spec.loader_version:
        env[version_var] = spec.loader_version
    return env


# ---- install -------------------------------------------------------------------------


def _safe_extract(z: zipfile.ZipFile, prefix: str, dest: Path, skip_top: set[str] = frozenset()) -> int:
    """Unpack members under `prefix` into dest. Ignores paths that would escape dest."""
    count = 0
    dest = dest.resolve()
    for info in z.infolist():
        name = info.filename
        if prefix and not name.startswith(prefix):
            continue
        rel = name[len(prefix) :].lstrip("/")
        if not rel or info.is_dir():
            continue
        parts = PurePosixPath(rel).parts
        if parts[0] in skip_top:
            continue
        target = (dest / rel).resolve()
        if not target.is_relative_to(dest):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)
        count += 1
    return count


def _server_pack_root(z: zipfile.ZipFile) -> str:
    """Server packs are sometimes wrapped in one top-level folder. Find where mods/ lives."""
    for name in z.namelist():
        parts = PurePosixPath(name).parts
        if "mods" in parts[:2]:
            i = parts.index("mods")
            return "/".join(parts[:i]) + ("/" if i else "")
    return ""


def _download(client: httpx.Client, url: str, dest: Path, limit: int = MAX_PACK_BYTES) -> None:
    tmp = dest.with_name(dest.name + ".part")
    size = 0
    try:
        with client.stream("GET", url) as resp, tmp.open("wb") as out:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise PackImportError("download is too large")
                out.write(chunk)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def _site_file_sync(client: httpx.Client, project_id: int | str, file_id: int | str) -> dict | None:
    try:
        resp = client.get(f"{CF_SITE_API}/mods/{project_id}/files/{file_id}")
        if resp.status_code == 200:
            return resp.json().get("data")
    except (httpx.HTTPError, ValueError):
        pass
    return None


def _server_pack_file(client: httpx.Client, project_id: str, file_id: str) -> dict | None:
    try:
        resp = client.get(f"{CF_SITE_API}/mods/{project_id}/files/{file_id}/additional-files")
        if resp.status_code != 200:
            return None
        for f in resp.json().get("data", []):
            if f.get("isServerPack") or not f.get("isCompatibleWithClient", True):
                return f
    except (httpx.HTTPError, ValueError):
        pass
    return None


def _cfwidget_project(client: httpx.Client, project_id: int) -> dict | None:
    try:
        resp = client.get(f"{CFWIDGET}/{project_id}")
        if resp.status_code == 200:
            return resp.json()
    except (httpx.HTTPError, ValueError):
        pass
    return None


def _modrinth_fallback(client: httpx.Client, slug: str, spec: PackSpec) -> dict | None:
    """The newest Modrinth file for a mod with the same slug, for this pack's MC + loader."""
    if not slug:
        return None
    try:
        params = {"game_versions": json.dumps([spec.minecraft_version]), "loaders": json.dumps([spec.loader])}
        resp = client.get(f"{MODRINTH}/project/{slug}/version", params=params)
        if resp.status_code != 200:
            return None
        for v in resp.json():
            files = v.get("files") or []
            f = next((f for f in files if f.get("primary")), files[0] if files else None)
            if f:
                return {"url": f["url"], "filename": f["filename"], "version": v.get("version_number", "")}
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return None


@dataclass
class ModResult:
    project_id: int
    file_id: int
    file_name: str = ""
    status: str = "ok"  # ok | client_only | modrinth | failed
    detail: str = ""


def _fetch_mod(client: httpx.Client, mods_dir: Path, spec: PackSpec, project_id: int, file_id: int) -> ModResult:
    r = ModResult(project_id, file_id)
    meta = _site_file_sync(client, project_id, file_id)
    if meta is None:
        # The website API didn't answer; cfwidget lists a project's files too.
        proj = _cfwidget_project(client, project_id)
        f = next((f for f in (proj or {}).get("files", []) if int(f.get("id", 0)) == file_id), None)
        if f:
            meta = {"fileName": f.get("name"), "gameVersions": f.get("versions", [])}
    else:
        proj = None

    if meta is not None:
        sides = {v.lower() for v in meta.get("gameVersions", [])}
        if "client" in sides and "server" not in sides:
            r.status, r.file_name = "client_only", meta.get("fileName", "")
            r.detail = "Tagged client-only on CurseForge"
            return r
        name = os.path.basename(meta.get("fileName") or "")
        if name:
            r.file_name = name
            try:
                if not (mods_dir / name).exists():
                    _download(client, cdn_url(file_id, name), mods_dir / name)
                return r
            except (httpx.HTTPError, OSError, PackImportError) as e:
                r.detail = str(e)

    # Last resort: the same mod on Modrinth.
    if proj is None:
        proj = _cfwidget_project(client, project_id)
    slug = ((proj or {}).get("urls", {}).get("curseforge", "") or "").rstrip("/").rsplit("/", 1)[-1]
    alt = _modrinth_fallback(client, slug, spec)
    if alt is not None:
        name = os.path.basename(alt["filename"])
        try:
            _download(client, alt["url"], mods_dir / name)
            r.status, r.file_name = "modrinth", name
            r.detail = f"Got {(proj or {}).get('title') or slug} {alt['version']} from Modrinth instead"
            return r
        except (httpx.HTTPError, OSError, PackImportError) as e:
            r.detail = str(e)
    r.status = "failed"
    title = (proj or {}).get("title") or f"project {project_id}"
    r.detail = f"Couldn't download {title} (file {file_id})" + (f": {r.detail}" if r.detail else "")
    return r


def _chown_tree(root: Path, uid: int | None, gid: int | None) -> None:
    if uid is None:
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for n in [*dirnames, *filenames, ""]:
            try:
                os.chown(os.path.join(dirpath, n) if n else dirpath, uid, gid if gid is not None else -1)
            except OSError:
                pass


def install(
    project_id: str,
    file_id: str,
    data_dir: str,
    uid: int | None = None,
    gid: int | None = None,
    progress: Callable[[str], None] = lambda _m: None,
    use_server_pack: bool = True,
) -> tuple[dict[str, str], list[str], bool]:
    """Download a CurseForge pack into a server folder.

    Returns (itzg env for the loader, notes for the user, whether a server pack was used).
    Raises PackImportError when nothing useful could be installed.
    """
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    cache = imports_dir() / "cache"
    cache.mkdir(exist_ok=True)
    notes: list[str] = []

    with _client() as client:
        # 1. The client pack zip (needed for its manifest even when there's a server pack).
        if project_id.startswith(UPLOAD_PREFIX):
            zip_path = upload_path(project_id)
        else:
            meta = _site_file_sync(client, project_id, file_id)
            name = (meta or {}).get("fileName")
            if not name:
                proj = _cfwidget_project(client, int(project_id)) or {}
                f = next((f for f in proj.get("files", []) if str(f.get("id")) == str(file_id)), None)
                name = (f or {}).get("name")
            if not name:
                raise PackImportError(f"Couldn't find file {file_id} of CurseForge project {project_id}.")
            zip_path = cache / f"{file_id}.zip"
            if not zip_path.exists():
                progress(f"Downloading {name}")
                try:
                    _download(client, cdn_url(file_id, name), zip_path)
                except (httpx.HTTPError, OSError) as e:
                    raise PackImportError(f"Couldn't download the pack from CurseForge's CDN: {e}") from e
        spec = parse_manifest(read_manifest(zip_path))
        env = loader_env(spec)

        # 2. A server pack, when the author made one.
        if use_server_pack and not project_id.startswith(UPLOAD_PREFIX):
            sp = _server_pack_file(client, project_id, file_id)
            if sp and sp.get("fileName"):
                sp_path = cache / f"{sp['id']}.zip"
                progress(f"Downloading the server pack {sp['fileName']}")
                try:
                    if not sp_path.exists():
                        _download(client, cdn_url(sp["id"], sp["fileName"]), sp_path)
                    with zipfile.ZipFile(sp_path) as z:
                        progress("Unpacking the server pack")
                        n = _safe_extract(z, _server_pack_root(z), root)
                    notes.append(f"Installed the author's server pack {sp['fileName']} ({n} files).")
                    sp_path.unlink(missing_ok=True)
                    if zip_path.parent == cache:
                        zip_path.unlink(missing_ok=True)
                    _chown_tree(root, uid, gid)
                    return env, notes, True
                except (httpx.HTTPError, OSError, zipfile.BadZipFile, PackImportError) as e:
                    log.warning("Server pack failed for %s/%s: %s", project_id, file_id, e)
                    notes.append(f"The server pack couldn't be used ({e}), so the mods were downloaded one by one.")
                    sp_path.unlink(missing_ok=True)

        # 3. Overrides (configs, scripts...) and every mod in the manifest.
        progress("Unpacking configs")
        with zipfile.ZipFile(zip_path) as z:
            _safe_extract(z, spec.overrides.rstrip("/") + "/", root)
        mods_dir = root / "mods"
        mods_dir.mkdir(exist_ok=True)
        total, done = len(spec.files), 0
        results: list[ModResult] = []
        progress(f"Downloading mods (0 of {total})")
        with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
            futures = [pool.submit(_fetch_mod, client, mods_dir, spec, p, f) for p, f in spec.files]
            for fut in futures:
                results.append(fut.result())
                done += 1
                if done % 10 == 0 or done == total:
                    progress(f"Downloading mods ({done} of {total})")

    if zip_path.parent == cache:
        zip_path.unlink(missing_ok=True)  # the pack is unpacked; keep uploads for rebuilds
    state = {
        "project_id": project_id,
        "file_id": file_id,
        "mods": [r.__dict__ for r in results],
    }
    (root / ".mcsm-import.json").write_text(json.dumps(state, indent=1))
    skipped = [r for r in results if r.status == "client_only"]
    swapped = [r for r in results if r.status == "modrinth"]
    failed = [r for r in results if r.status == "failed"]
    ok = total - len(failed) - len(skipped)
    notes.append(f"Downloaded {ok} of {total} mods.")
    if skipped:
        notes.append(f"Skipped {len(skipped)} mods CurseForge marks as client-only.")
    notes.extend(r.detail for r in swapped)
    notes.extend(r.detail for r in failed)
    if total and len(failed) == total:
        raise PackImportError("None of the pack's mods could be downloaded. " + (failed[0].detail if failed else ""))
    _chown_tree(root, uid, gid)
    return env, notes, False
