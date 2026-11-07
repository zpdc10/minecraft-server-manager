"""Runtime settings, read from environment variables.

All settings live here so new features (server management, CurseForge)
add their variables in one place.
"""

import os
from dataclasses import dataclass, field

APP_VERSION = "0.1.0"
USER_AGENT = f"zpdc10/minecraft-server-manager/{APP_VERSION}"


@dataclass(frozen=True)
class Settings:
    modrinth_api_url: str = field(
        default_factory=lambda: os.environ.get("MODRINTH_API_URL", "https://api.modrinth.com/v2")
    )
    # CurseForge key fallback. A key saved in the UI wins; use
    # settings_store.curseforge_api_key() to read the effective value.
    curseforge_api_key: str = field(default_factory=lambda: os.environ.get("CF_API_KEY", ""))
    curseforge_api_url: str = field(
        default_factory=lambda: os.environ.get("CURSEFORGE_API_URL", "https://api.curseforge.com")
    )
    # Where UI-editable settings (settings.json) are stored.
    data_dir: str = field(default_factory=lambda: os.environ.get("DATA_DIR", "/data"))
    # --- Server management ---
    # Where server data folders live, as seen from inside this app's container.
    # Each server gets <data_root>/<server id>, mounted as /data in its container.
    data_root: str = field(default_factory=lambda: os.environ.get("DATA_ROOT", "/data/servers"))
    # The same folder as seen from the Docker host. Minecraft containers are created by
    # the host's Docker daemon, so their bind mounts need host paths. When empty, the app
    # works it out from its own container's mounts, or assumes the paths are the same.
    host_data_root: str = field(default_factory=lambda: os.environ.get("HOST_DATA_ROOT", ""))
    # Optional Docker network to attach Minecraft containers to.
    docker_network: str = field(default_factory=lambda: os.environ.get("DOCKER_NETWORK", ""))
    # Image used for Minecraft servers. A Java tag such as ":java17" is appended.
    mc_image: str = field(default_factory=lambda: os.environ.get("MC_IMAGE", "itzg/minecraft-server"))
    # Prefix for container names, so we never touch containers we didn't create.
    container_prefix: str = field(default_factory=lambda: os.environ.get("CONTAINER_PREFIX", "mcsm-"))
    # User and group the Minecraft server runs as. Unraid's appdata convention is 99:100.
    mc_uid: int = field(default_factory=lambda: int(os.environ.get("MC_UID", "99")))
    mc_gid: int = field(default_factory=lambda: int(os.environ.get("MC_GID", "100")))
    # --- Server builder (test-start a modpack server and remove mods that break it) ---
    # Shared list of client-only / crashing mods. Empty uses only the copy in the image.
    mod_db_url: str = field(
        default_factory=lambda: os.environ.get(
            "MOD_DB_URL",
            "https://raw.githubusercontent.com/zpdc10/minecraft-server-manager/main/backend/app/data/known-mods.json",
        )
    )
    build_max_attempts: int = field(default_factory=lambda: int(os.environ.get("BUILD_MAX_ATTEMPTS", "10")))
    # Most mods the builder may remove because of crashes in one build.
    build_max_removals: int = field(default_factory=lambda: int(os.environ.get("BUILD_MAX_REMOVALS", "15")))
    build_install_timeout: int = field(default_factory=lambda: int(os.environ.get("BUILD_INSTALL_TIMEOUT", "3600")))
    build_start_timeout: int = field(default_factory=lambda: int(os.environ.get("BUILD_START_TIMEOUT", "1200")))
    static_dir: str = field(default_factory=lambda: os.environ.get("STATIC_DIR", "/app/static"))


settings = Settings()
