"""Models for importing a CurseForge pack from a link or an uploaded zip (no API key)."""

from pydantic import BaseModel, Field


class ImportFile(BaseModel):
    id: str
    name: str = ""
    display_name: str = ""
    minecraft_version: str = ""
    loader: str = ""
    release_type: str = "release"
    uploaded_at: str = ""
    size: int = 0
    mod_count: int | None = None


class ImportPreview(BaseModel):
    # CurseForge project id, or "upload-<id>" for an uploaded zip.
    project_id: str
    slug: str = ""
    title: str
    summary: str = ""
    icon_url: str | None = None
    url: str = ""
    files: list[ImportFile] = Field(default_factory=list)
    selected_file_id: str = ""
    # None when it couldn't be checked.
    selected_has_server_pack: bool | None = None


class LinkRequest(BaseModel):
    url: str


class ServerPackCheck(BaseModel):
    has_server_pack: bool | None = None
