"""Mod loader versions available for a Minecraft version, newest first.

Pack exports must name an exact loader version, so the builder picks one here.
"""

import re
import time
import xml.etree.ElementTree as ET

import httpx

from ..config import USER_AGENT
from ..providers import ProviderError

FABRIC_META = "https://meta.fabricmc.net/v2/versions/loader/{mc}"
QUILT_META = "https://meta.quiltmc.org/v3/versions/loader/{mc}"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
NEOFORGE_MAVEN = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"

CACHE_SECONDS = 3600
_cache: dict[str, tuple[float, object]] = {}


async def _fetch(url: str, as_json: bool = True):
    now = time.time()
    hit = _cache.get(url)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True) as client:
        resp = await client.get(url)
    if resp.status_code == 404:
        data = [] if as_json else ""
    else:
        resp.raise_for_status()
        data = resp.json() if as_json else resp.text
    _cache[url] = (now, data)
    return data


def neoforge_prefix(mc: str) -> str | None:
    """NeoForge versions encode the MC version: 1.21.1 -> "21.1.", 1.21 -> "21.0."."""
    m = re.match(r"^1\.(\d+)(?:\.(\d+))?$", mc)
    if not m:
        return None
    return f"{m.group(1)}.{m.group(2) or 0}."


def _version_key(v: str) -> list:
    return [int(p) if p.isdigit() else -1 for p in re.split(r"[.\-+]", v)]


async def loader_versions(loader: str, mc: str) -> list[str]:
    """All versions of `loader` that run on Minecraft `mc`, recommended/newest first."""
    if loader == "fabric":
        data = await _fetch(FABRIC_META.format(mc=mc))
        stable = [d["loader"]["version"] for d in data if d["loader"].get("stable")]
        rest = [d["loader"]["version"] for d in data if not d["loader"].get("stable")]
        return stable + rest
    if loader == "quilt":
        data = await _fetch(QUILT_META.format(mc=mc))
        versions = [d["loader"]["version"] for d in data]
        stable = sorted((v for v in versions if "beta" not in v), key=_version_key, reverse=True)
        beta = sorted((v for v in versions if "beta" in v), key=_version_key, reverse=True)
        return stable + beta
    if loader == "forge":
        xml = await _fetch(FORGE_MAVEN, as_json=False)
        if not xml:
            return []
        prefix = f"{mc}-"
        versions = [
            v.text[len(prefix) :]
            for v in ET.fromstring(xml).iter("version")
            if v.text and v.text.startswith(prefix) and v.text.count("-") == 1
        ]
        versions.sort(key=_version_key, reverse=True)
        try:
            promos = (await _fetch(FORGE_PROMOS)).get("promos", {})
        except httpx.HTTPError:
            promos = {}
        recommended = promos.get(f"{mc}-recommended")
        if recommended in versions:
            versions.remove(recommended)
            versions.insert(0, recommended)
        return versions
    if loader == "neoforge":
        prefix = neoforge_prefix(mc)
        if prefix is None:
            return []
        data = await _fetch(NEOFORGE_MAVEN)
        versions = [v for v in data.get("versions", []) if v.startswith(prefix)] if data else []
        stable = [v for v in versions if "beta" not in v]
        beta = [v for v in versions if "beta" in v]
        return sorted(stable, key=_version_key, reverse=True) + sorted(beta, key=_version_key, reverse=True)
    raise ProviderError(f"Unknown loader '{loader}'", status_code=400)


async def default_loader_version(loader: str, mc: str) -> str:
    try:
        versions = await loader_versions(loader, mc)
    except httpx.HTTPError as exc:
        raise ProviderError(f"Could not look up {loader} versions: {exc}") from exc
    if not versions:
        raise ProviderError(f"{loader} has no release for Minecraft {mc}.", status_code=400)
    return versions[0]
