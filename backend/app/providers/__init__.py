"""Modpack providers. Each implements providers.base.ModpackProvider.

Register a new provider by adding it to PROVIDERS below.
"""

from .base import ModpackProvider, ProviderError
from .curseforge import CurseForgeProvider
from .modrinth import ModrinthProvider

PROVIDERS: dict[str, ModpackProvider] = {
    "modrinth": ModrinthProvider(),
    "curseforge": CurseForgeProvider(),
}

__all__ = ["PROVIDERS", "ModpackProvider", "ProviderError"]
