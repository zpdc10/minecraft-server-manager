"""Interface every modpack provider implements."""

from typing import Protocol

from ..models import FilterOptions, PackVersion, SearchResult


class ModpackProvider(Protocol):
    name: str

    async def search(
        self,
        query: str = "",
        game_version: str | None = None,
        loader: str | None = None,
        category: str | None = None,
        sort: str = "relevance",
        offset: int = 0,
        limit: int = 20,
    ) -> SearchResult: ...

    async def filter_options(self) -> FilterOptions: ...

    async def get_versions(self, project_id: str) -> list[PackVersion]: ...
