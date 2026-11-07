"""Build environment variables for the itzg/minecraft-server image.

Each pack source has its own builder in SOURCE_ENV_BUILDERS. Add a new source by
writing a builder and registering it there.
See https://docker-minecraft-server.readthedocs.io/ for the variables.
"""

import re
from collections.abc import Callable

from .. import settings_store
from ..config import Settings
from ..models.server import CreateServerRequest


class EnvBuildError(ValueError):
    """The request can't be turned into a working server config."""


def _modrinth_env(req: CreateServerRequest, settings: Settings) -> dict[str, str]:
    pack = req.project_slug or req.project_id
    if not pack:
        raise EnvBuildError("Modrinth servers need a project_id or project_slug")
    env = {"TYPE": "MODRINTH", "MODRINTH_MODPACK": pack}
    if req.version_id:
        env["MODRINTH_VERSION"] = req.version_id
    if req.loader:
        env["MODRINTH_LOADER"] = req.loader.lower()
    return env


def _curseforge_env(req: CreateServerRequest, settings: Settings) -> dict[str, str]:
    # A key saved on the Settings page wins over the CF_API_KEY env var.
    api_key = settings_store.curseforge_api_key()
    if not api_key:
        raise EnvBuildError("A CurseForge API key is required for CurseForge packs. Add one in Settings.")
    if not req.project_slug:
        raise EnvBuildError("CurseForge servers need a project_slug")
    env = {
        "TYPE": "AUTO_CURSEFORGE",
        # Passed via the Docker API, so no $$ escaping is needed.
        "CF_API_KEY": api_key,
        "CF_SLUG": req.project_slug,
    }
    if req.version_id:
        env["CF_FILE_ID"] = req.version_id
    return env


def _vanilla_env(req: CreateServerRequest, settings: Settings) -> dict[str, str]:
    return {"TYPE": "VANILLA", "VERSION": req.minecraft_version or "LATEST"}


# itzg TYPE and the variable that pins the loader version, per pack loader.
CUSTOM_LOADER_ENV = {
    "fabric": ("FABRIC", "FABRIC_LOADER_VERSION"),
    "quilt": ("QUILT", "QUILT_LOADER_VERSION"),
    "forge": ("FORGE", "FORGE_VERSION"),
    "neoforge": ("NEOFORGE", "NEOFORGE_VERSION"),
}


def _custom_env(req: CreateServerRequest, settings: Settings) -> dict[str, str]:
    """A pack made in the pack builder. Its mods are copied into /data/mods before start."""
    from .pack_store import PackNotFound, get

    try:
        pack = get(req.project_id)
    except PackNotFound as e:
        raise EnvBuildError(f"Custom pack '{req.project_id}' not found") from e
    if not pack.loader_version:
        raise EnvBuildError(f"Pick a {pack.loader} version for the pack first")
    server_type, version_var = CUSTOM_LOADER_ENV[pack.loader]
    return {"TYPE": server_type, "VERSION": pack.minecraft_version, version_var: pack.loader_version}


def _cfimport_env(req: CreateServerRequest, settings: Settings) -> dict[str, str]:
    """A CurseForge pack imported from a link or upload, without an API key.

    The pack is downloaded into /data before the container starts, and the loader
    variables (TYPE, VERSION, FORGE_VERSION...) are added from its manifest then.
    """
    from .cf_import import UPLOAD_PREFIX

    pid = req.project_id
    if not (pid.isdigit() or pid.startswith(UPLOAD_PREFIX)):
        raise EnvBuildError("Imported CurseForge servers need a project id or an uploaded pack")
    if pid.isdigit() and not req.version_id.isdigit():
        raise EnvBuildError("Pick which file of the pack to install")
    return {}


SOURCE_ENV_BUILDERS: dict[str, Callable[[CreateServerRequest, Settings], dict[str, str]]] = {
    "modrinth": _modrinth_env,
    "curseforge": _curseforge_env,
    "vanilla": _vanilla_env,
    "custom": _custom_env,
    "cfimport": _cfimport_env,
}


def _parse_mc_version(v: str) -> tuple[int, int, int] | None:
    m = re.match(r"^1\.(\d+)(?:\.(\d+))?", v.strip())
    if not m:
        return None
    return (1, int(m.group(1)), int(m.group(2) or 0))


def pick_java_version(minecraft_version: str) -> str:
    """Pick the itzg image Java tag that suits a Minecraft version."""
    parsed = _parse_mc_version(minecraft_version or "")
    if parsed is None:
        return "latest"
    _, minor, patch = parsed
    if minor <= 16:
        return "java8"  # old Forge packs only run reliably on Java 8
    if minor == 17:
        return "java16"
    if minor < 20 or (minor == 20 and patch < 5):
        return "java17"
    return "java21"


def build_env(req: CreateServerRequest, settings: Settings) -> dict[str, str]:
    builder = SOURCE_ENV_BUILDERS.get(req.source)
    if builder is None:
        raise EnvBuildError(f"Unsupported source: {req.source}")
    env = {
        "EULA": "TRUE",
        "MEMORY": f"{req.memory_gb}G",
        "USE_AIKAR_FLAGS": "true",
        "MOTD": req.name,
        "SERVER_NAME": req.name,
        "ENABLE_RCON": "true",
    }
    env.update(builder(req, settings))
    # User-provided extras win, except they can't turn off the EULA flag.
    env.update({k: v for k, v in req.extra_env.items() if k != "EULA"})
    return env
