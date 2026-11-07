"""Models for the server management tools (console, players, config, settings)."""

from pydantic import BaseModel, Field, field_validator

from .server import JavaVersion


class ConsoleRequest(BaseModel):
    command: str = Field(min_length=1, max_length=1000)


class ConsoleResult(BaseModel):
    command: str
    output: str


class PlayerEntry(BaseModel):
    name: str
    uuid: str = ""
    level: int | None = None
    reason: str = ""


class PlayerLists(BaseModel):
    running: bool = False
    # RCON answered, so commands can be sent.
    console_ready: bool = False
    online: list[str] = Field(default_factory=list)
    online_count: int = 0
    max_players: int = 0
    whitelist_enabled: bool = False
    whitelist: list[PlayerEntry] = Field(default_factory=list)
    ops: list[PlayerEntry] = Field(default_factory=list)
    banned: list[PlayerEntry] = Field(default_factory=list)
    banned_ips: list[PlayerEntry] = Field(default_factory=list)


class PlayerActionRequest(BaseModel):
    # whitelist_add | whitelist_remove | op | deop | kick | ban | pardon | ban_ip | pardon_ip
    action: str
    name: str = Field(min_length=1, max_length=64)
    reason: str = ""


class WhitelistToggle(BaseModel):
    enabled: bool


class PropertiesInfo(BaseModel):
    exists: bool
    properties: dict[str, str] = Field(default_factory=dict)
    # Keys that can't be edited, with the reason.
    locked: dict[str, str] = Field(default_factory=dict)


class PropertiesUpdate(BaseModel):
    properties: dict[str, str]
    restart: bool = False


class UpdateSettingsRequest(BaseModel):
    """Settings that need the container recreated. Omitted fields stay the same."""

    name: str | None = Field(default=None, min_length=1, max_length=48)
    port: int | None = Field(default=None, ge=1024, le=65535)
    memory_gb: int | None = Field(default=None, ge=1, le=64)
    java_version: JavaVersion | None = None
    motd: str | None = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("name must not be blank")
        return v


class ServerStats(BaseModel):
    running: bool
    cpu_percent: float = 0.0
    cpu_count: int = 0
    memory_used_mb: int = 0
    memory_limit_mb: int = 0
    started_at: str = ""


class ModFile(BaseModel):
    file: str
    enabled: bool
    size: int
    modified: str = ""
