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
    static_dir: str = field(default_factory=lambda: os.environ.get("STATIC_DIR", "/app/static"))


settings = Settings()
