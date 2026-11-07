"""Source-agnostic data shapes shared by every modpack provider."""

from typing import Literal

from pydantic import BaseModel

Source = Literal["modrinth", "curseforge"]


class Modpack(BaseModel):
    source: Source
    id: str
    slug: str
    title: str
    description: str
    author: str
    icon_url: str | None = None
    downloads: int
    follows: int
    loaders: list[str]
    categories: list[str]
    game_versions: list[str]
    updated: str | None = None
    url: str


class SearchResult(BaseModel):
    hits: list[Modpack]
    total: int
    offset: int
    limit: int


class FilterOptions(BaseModel):
    game_versions: list[str]
    loaders: list[str]
    categories: list[str]


class PackVersion(BaseModel):
    """One installable version of a modpack (what a server is created from)."""

    id: str
    name: str
    version_number: str
    game_versions: list[str]
    loaders: list[str]
    published: str | None = None
