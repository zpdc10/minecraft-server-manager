"""Models for custom packs: a list of mods the user assembles in the pack builder."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .modpack import Modpack, Source

PackLoader = Literal["fabric", "forge", "neoforge", "quilt"]
# Modrinth's side support values, plus "unknown" for CurseForge, which has no side data.
Side = Literal["required", "optional", "unsupported", "unknown"]


def mod_key(source: str, project_id: str) -> str:
    return f"{source}:{project_id}"


class ModHit(Modpack):
    """A mod in search results. Same shape as a modpack, plus side support."""

    client_side: Side = "unknown"
    server_side: Side = "unknown"


class ModSearchResult(BaseModel):
    hits: list[ModHit]
    total: int
    offset: int
    limit: int


class ModVersion(BaseModel):
    """One downloadable file of a mod, with what it depends on."""

    source: Source
    project_id: str
    id: str
    name: str
    version_number: str = ""
    game_versions: list[str] = Field(default_factory=list)
    loaders: list[str] = Field(default_factory=list)
    release_type: str = "release"  # release, beta or alpha
    published: str | None = None
    file_name: str = ""
    download_url: str | None = None
    sha1: str = ""
    sha512: str = ""
    size: int = 0
    # Keys (source:project_id) of required dependencies and of mods this one can't run with.
    requires: list[str] = Field(default_factory=list)
    incompatible: list[str] = Field(default_factory=list)


class PackMod(BaseModel):
    source: Source
    project_id: str
    slug: str = ""
    title: str
    icon_url: str | None = None
    url: str = ""
    version_id: str
    version_number: str = ""
    game_versions: list[str] = Field(default_factory=list)
    loaders: list[str] = Field(default_factory=list)
    file_name: str
    download_url: str | None = None
    sha1: str = ""
    sha512: str = ""
    size: int = 0
    client_side: Side = "unknown"
    server_side: Side = "unknown"
    # True when the builder added this mod because another mod needs it.
    added_as_dependency: bool = False
    # Keys of mods in the pack that need this one.
    required_by: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    incompatible: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return mod_key(self.source, self.project_id)


class CustomPack(BaseModel):
    id: str
    name: str
    version: str = "1.0.0"
    author: str = ""
    summary: str = ""
    minecraft_version: str
    loader: PackLoader
    loader_version: str = ""
    mods: list[PackMod] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def find(self, key: str) -> PackMod | None:
        return next((m for m in self.mods if m.key == key), None)


class PackIssue(BaseModel):
    severity: Literal["error", "warning", "info"]
    message: str
    mod: str | None = None  # key of the mod the issue is about


class PackView(CustomPack):
    """A pack as the API returns it: the stored pack plus its current problems."""

    issues: list[PackIssue] = Field(default_factory=list)


class CreatePackRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    minecraft_version: str = Field(min_length=1)
    loader: PackLoader
    loader_version: str = ""
    author: str = ""
    summary: str = ""

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class UpdatePackRequest(BaseModel):
    """Fields to change. Changing the Minecraft version or loader re-picks every mod's version."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    version: str | None = Field(default=None, min_length=1, max_length=32)
    author: str | None = None
    summary: str | None = None
    minecraft_version: str | None = None
    loader: PackLoader | None = None
    loader_version: str | None = None


class AddModRequest(BaseModel):
    source: Source
    project_id: str = Field(min_length=1)
    # A specific version/file id. Empty picks the newest one that fits the pack.
    version_id: str = ""


class AddModResult(BaseModel):
    pack: PackView
    added: list[str]  # titles of mods that were added, the requested one first
    notes: list[str] = Field(default_factory=list)


class RetargetResult(BaseModel):
    pack: PackView
    updated: list[str]
    unavailable: list[str]  # mods with no version for the new target, left as they were
