"""Models for checking servers for newer modpack versions and updating them."""

from pydantic import BaseModel, Field

from .server import BuildInfo


class UpdateVersion(BaseModel):
    """One version of a server's modpack."""

    id: str
    name: str
    minecraft_version: str = ""
    loader: str = ""
    published: str = ""
    # release | beta | alpha
    release_type: str = "release"
    # Moves the server to another Minecraft version (worlds can't go back after that).
    changes_minecraft: bool = False


class PackLink(BaseModel):
    """Where an imported CurseForge pack came from, kept so it can be checked again."""

    url: str = ""
    project_id: str = ""
    slug: str = ""
    title: str = ""
    # Matched by the pack's name rather than given by the user.
    guessed: bool = False


class UpdateCheck(BaseModel):
    server_id: str
    # False when this server's pack can't be checked (the reason says why).
    supported: bool = True
    reason: str = ""
    current_version_id: str = ""
    current_name: str = ""
    # The version the app suggests: the newest release for the same Minecraft version.
    latest: UpdateVersion | None = None
    # Every version newer than the current one, newest first.
    newer: list[UpdateVersion] = Field(default_factory=list)
    update_available: bool = False
    checked_at: str = ""
    error: str = ""
    link: PackLink | None = None


class UpdateJob(BaseModel):
    """A running or finished update."""

    # backing-up | stopping | installing | testing | rolling-back | passed | rolled-back | failed
    phase: str
    message: str = ""
    from_version: str = ""
    to_version: str = ""
    to_version_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    backup_id: str = ""
    # The test-start report of the new version (kept when it failed and was rolled back).
    build: BuildInfo | None = None


class ServerUpdates(BaseModel):
    server_id: str
    check: UpdateCheck | None = None
    job: UpdateJob | None = None
    history: list[UpdateJob] = Field(default_factory=list)
    link: PackLink | None = None


class UpdateRequest(BaseModel):
    version_id: str = Field(min_length=1, max_length=100)


class LinkUpdate(BaseModel):
    url: str = Field(min_length=1, max_length=500)
