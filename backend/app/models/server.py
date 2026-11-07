"""Request and response models for managed Minecraft servers."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ServerSource = Literal["modrinth", "curseforge", "vanilla", "custom"]
JavaVersion = Literal["java8", "java11", "java16", "java17", "java21", "latest"]
ServerStatus = Literal[
    "pulling",  # downloading the server image
    "creating",  # container being created
    "starting",  # container running, server installing or booting
    "running",  # server is up and healthy
    "stopping",
    "stopped",
    "error",
    "unknown",
]


class CreateServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    source: ServerSource = "modrinth"
    # Modrinth project id/slug, CurseForge project id, or custom pack id. Unused for vanilla.
    project_id: str = ""
    # Slug of the project (CurseForge needs this for CF_SLUG).
    project_slug: str = ""
    # Modrinth version id or CurseForge file id. Empty means latest.
    version_id: str = ""
    pack_title: str = ""
    # Minecraft version, used to pick a Java version and for vanilla servers.
    minecraft_version: str = ""
    loader: str = ""
    port: int = Field(default=25565, ge=1024, le=65535)
    memory_gb: int = Field(default=4, ge=1, le=64)
    java_version: JavaVersion | None = None
    extra_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class ServerInfo(BaseModel):
    id: str
    name: str
    source: str
    project_id: str = ""
    project_slug: str = ""
    version_id: str = ""
    pack_title: str = ""
    minecraft_version: str = ""
    loader: str = ""
    port: int
    memory_gb: int
    java_version: str
    status: ServerStatus
    container_name: str
    data_path: str
    created_at: str = ""
    error: str = ""
