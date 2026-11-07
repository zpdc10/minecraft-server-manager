"""Read mod metadata out of the jars in a server's mods folder.

For each jar we collect its mod ids and display name, whether it declares itself
client-only, the Java packages it ships and its mixin configs. The crash analyzer
uses the packages and mixin configs to map a stack trace back to the jar.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
import zipfile
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_TOML_MODID = re.compile(r'^\s*modId\s*=\s*"([^"]+)"', re.M)
_TOML_NAME = re.compile(r'^\s*displayName\s*=\s*"([^"]+)"', re.M)
_TOML_CLIENT_ONLY = re.compile(r"^\s*clientSideOnly\s*=\s*true", re.M)


@dataclass
class ModJar:
    filename: str
    mod_ids: list[str] = field(default_factory=list)
    name: str = ""
    # The jar's own metadata says it only runs on the client.
    client_only: bool = False
    packages: set[str] = field(default_factory=set)
    mixin_configs: set[str] = field(default_factory=set)

    @property
    def label(self) -> str:
        return self.name or (self.mod_ids[0] if self.mod_ids else self.filename)


def _loads_json(raw: bytes) -> dict:
    # Mod authors ship fabric.mod.json with stray control characters and comments.
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    data = json.loads(text, strict=False)
    return data if isinstance(data, dict) else {}


def _read_fabric(jar: ModJar, raw: bytes) -> None:
    try:
        data = _loads_json(raw)
    except ValueError:
        m = re.search(rb'"id"\s*:\s*"([^"]+)"', raw)
        if m:
            jar.mod_ids.append(m.group(1).decode())
        return
    if data.get("id"):
        jar.mod_ids.append(str(data["id"]))
    jar.name = jar.name or str(data.get("name") or "")
    if str(data.get("environment", "*")).lower() == "client":
        jar.client_only = True
    mixins = data.get("mixins") or []
    for m in mixins:
        cfg = m.get("config") if isinstance(m, dict) else m
        if isinstance(cfg, str):
            jar.mixin_configs.add(cfg)


def _read_quilt(jar: ModJar, raw: bytes) -> None:
    try:
        data = _loads_json(raw)
    except ValueError:
        return
    ql = data.get("quilt_loader") or {}
    if ql.get("id"):
        jar.mod_ids.append(str(ql["id"]))
    jar.name = jar.name or str((ql.get("metadata") or {}).get("name") or "")
    env = (data.get("minecraft") or {}).get("environment", "*")
    if str(env).lower() == "client":
        jar.client_only = True
    mixins = data.get("mixin") or []
    for cfg in [mixins] if isinstance(mixins, str) else mixins:
        if isinstance(cfg, str):
            jar.mixin_configs.add(cfg)


def _read_forge_toml(jar: ModJar, raw: bytes) -> None:
    text = raw.decode("utf-8", errors="replace")
    try:
        data = tomllib.loads(text)
        mods = data.get("mods") or []
        for m in mods:
            if m.get("modId"):
                jar.mod_ids.append(str(m["modId"]))
                jar.name = jar.name or str(m.get("displayName") or "")
        if data.get("clientSideOnly") is True:
            jar.client_only = True
        for m in data.get("mixins") or []:
            if isinstance(m, dict) and isinstance(m.get("config"), str):
                jar.mixin_configs.add(m["config"])
    except (tomllib.TOMLDecodeError, AttributeError, TypeError):
        # Some mods.toml files aren't valid TOML; fall back to regexes.
        jar.mod_ids += _TOML_MODID.findall(text)
        names = _TOML_NAME.findall(text)
        jar.name = jar.name or (names[0] if names else "")
        if _TOML_CLIENT_ONLY.search(text):
            jar.client_only = True


def read_jar(path: str) -> ModJar:
    jar = ModJar(filename=os.path.basename(path))
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            for meta, reader in (
                ("fabric.mod.json", _read_fabric),
                ("quilt.mod.json", _read_quilt),
                ("META-INF/neoforge.mods.toml", _read_forge_toml),
                ("META-INF/mods.toml", _read_forge_toml),
            ):
                if meta in names:
                    reader(jar, z.read(meta))
            for n in names:
                if n.endswith(".class") and "/" in n and not n.startswith("META-INF/"):
                    parts = n.split("/")[:-1]
                    # Keep up to three levels (com/example/mod) as the package key.
                    jar.packages.add(".".join(parts[:3]))
                elif "/" not in n and n.endswith(".json") and "mixin" in n.lower():
                    jar.mixin_configs.add(n)
    except (zipfile.BadZipFile, OSError, KeyError) as e:
        log.warning("Could not read %s: %s", path, e)
    # mods.toml and fabric.mod.json can both list an id; keep order, drop dupes.
    jar.mod_ids = list(dict.fromkeys(i.strip() for i in jar.mod_ids if i.strip()))
    return jar


def scan_mods(mods_dir: str) -> list[ModJar]:
    if not os.path.isdir(mods_dir):
        return []
    return [read_jar(os.path.join(mods_dir, f)) for f in sorted(os.listdir(mods_dir)) if f.lower().endswith(".jar")]
