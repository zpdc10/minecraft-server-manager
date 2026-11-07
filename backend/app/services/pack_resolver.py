"""Add and remove mods in a custom pack, resolving dependencies, and check the result.

Rules:
- A mod is added at the newest stable version that supports the pack's Minecraft
  version and loader. Its required dependencies are added the same way, recursively.
- A dependency that is already in the pack, from either source (matched by slug), is
  reused rather than added twice.
- Removing a mod also removes dependencies that were only added for it.
"""

from ..models.pack import (
    AddModRequest,
    CustomPack,
    ModVersion,
    PackIssue,
    PackMod,
    PackView,
    mod_key,
)
from ..providers import ProviderError
from ..providers.mods import LOADER_COMPAT, MOD_SOURCES, ModInfo, pick_best

MAX_NEW_MODS = 100  # guard against runaway dependency chains


def fits(pack: CustomPack, game_versions: list[str], loaders: list[str]) -> bool:
    if game_versions and pack.minecraft_version not in game_versions:
        return False
    ok = LOADER_COMPAT.get(pack.loader, [pack.loader])
    # Old CurseForge files sometimes carry no loader tag; don't reject those.
    return not loaders or any(lo in ok for lo in loaders)


def _by_slug(pack: CustomPack, slug: str) -> PackMod | None:
    return next((m for m in pack.mods if slug and m.slug == slug), None)


def _to_pack_mod(info: ModInfo, v: ModVersion, as_dependency: bool) -> PackMod:
    return PackMod(
        source=info.source,
        project_id=info.id,
        slug=info.slug,
        title=info.title,
        icon_url=info.icon_url,
        url=info.url,
        version_id=v.id,
        version_number=v.version_number,
        game_versions=v.game_versions,
        loaders=v.loaders,
        file_name=v.file_name,
        download_url=v.download_url,
        sha1=v.sha1,
        sha512=v.sha512,
        size=v.size,
        client_side=info.client_side,
        server_side=info.server_side,
        added_as_dependency=as_dependency,
        requires=v.requires,
        incompatible=v.incompatible,
    )


async def _best_version(pack: CustomPack, source: str, project_id: str) -> ModVersion | None:
    versions = await MOD_SOURCES[source].versions(project_id, pack.minecraft_version, pack.loader)
    return pick_best([v for v in versions if fits(pack, v.game_versions, v.loaders)])


async def add_mod(pack: CustomPack, req: AddModRequest) -> tuple[list[str], list[str]]:
    """Add a mod and its required dependencies to `pack` in place.

    Returns (titles added, notes). Raises ProviderError when the requested mod
    itself can't be added; problems with dependencies become notes instead.
    """
    source = MOD_SOURCES[req.source]
    info = await source.project(req.project_id)
    key = mod_key(info.source, info.id)
    if pack.find(key):
        raise ProviderError(f"{info.title} is already in this pack.", status_code=409)
    same = _by_slug(pack, info.slug)
    if same:
        raise ProviderError(f"{info.title} is already in this pack (from {same.source}).", status_code=409)

    if req.version_id:
        version = await source.version(req.version_id, info.id)
        if not fits(pack, version.game_versions, version.loaders):
            raise ProviderError(
                f"{version.name} doesn't support Minecraft {pack.minecraft_version} with {pack.loader}.",
                status_code=400,
            )
    else:
        version = await _best_version(pack, info.source, info.id)
        if version is None:
            raise ProviderError(
                f"{info.title} has no version for Minecraft {pack.minecraft_version} with {pack.loader}.",
                status_code=400,
            )

    added: list[str] = []
    notes: list[str] = []
    root = _to_pack_mod(info, version, as_dependency=False)
    pack.mods.append(root)
    added.append(root.title)
    if info.server_side == "unsupported":
        notes.append(f"{info.title} is client-only, so it is left out of servers made from this pack.")

    queue: list[tuple[PackMod, str]] = [(root, dep) for dep in root.requires]
    while queue:
        parent, dep_key = queue.pop(0)
        existing = pack.find(dep_key)
        if existing is None:
            dep_source, dep_id = dep_key.split(":", 1)
            try:
                dep_info = await MOD_SOURCES[dep_source].project(dep_id)
            except ProviderError as e:
                notes.append(f"Could not look up a dependency of {parent.title}: {e.message}")
                continue
            existing = pack.find(mod_key(dep_info.source, dep_info.id)) or _by_slug(pack, dep_info.slug)
            if existing is None:
                if len(added) > MAX_NEW_MODS:
                    notes.append("Stopped adding dependencies: too many.")
                    break
                dep_version = await _best_version(pack, dep_info.source, dep_info.id)
                if dep_version is None:
                    notes.append(
                        f"{parent.title} needs {dep_info.title}, which has no version for "
                        f"Minecraft {pack.minecraft_version} with {pack.loader}."
                    )
                    continue
                existing = _to_pack_mod(dep_info, dep_version, as_dependency=True)
                pack.mods.append(existing)
                added.append(existing.title)
                queue.extend((existing, d) for d in existing.requires)
            # Point the parent at the copy that's really in the pack (may be the other source).
            parent.requires = [existing.key if k == dep_key else k for k in parent.requires]
        if parent.key not in existing.required_by:
            existing.required_by.append(parent.key)

    for m in pack.mods:
        if m.key in root.incompatible or root.key in m.incompatible:
            notes.append(f"{root.title} is marked incompatible with {m.title}.")
    return added, notes


def remove_mod(pack: CustomPack, key: str) -> list[str]:
    """Remove a mod, then any dependencies nothing else needs. Returns titles removed."""
    target = pack.find(key)
    if target is None:
        raise ProviderError("That mod is not in this pack.", status_code=404)
    removed: list[str] = []
    to_remove = [target]
    while to_remove:
        mod = to_remove.pop()
        if mod not in pack.mods:
            continue
        pack.mods.remove(mod)
        removed.append(mod.title)
        for other in pack.mods:
            if mod.key in other.required_by:
                other.required_by.remove(mod.key)
                if other.added_as_dependency and not other.required_by:
                    to_remove.append(other)
    return removed


def check(pack: CustomPack) -> list[PackIssue]:
    issues: list[PackIssue] = []
    keys = {m.key for m in pack.mods}
    titles = {m.key: m.title for m in pack.mods}
    if not pack.loader_version:
        issues.append(PackIssue(severity="error", message=f"Pick a {pack.loader} version."))
    seen_slugs: dict[str, PackMod] = {}
    for m in pack.mods:
        if not fits(pack, m.game_versions, m.loaders):
            issues.append(
                PackIssue(
                    severity="error",
                    mod=m.key,
                    message=f"{m.title} {m.version_number} doesn't support Minecraft "
                    f"{pack.minecraft_version} with {pack.loader}.",
                )
            )
        for dep in m.requires:
            if dep not in keys:
                issues.append(
                    PackIssue(severity="error", mod=m.key, message=f"{m.title} is missing a required dependency.")
                )
        for bad in m.incompatible:
            if bad in keys:
                issues.append(
                    PackIssue(severity="error", mod=m.key, message=f"{m.title} is incompatible with {titles[bad]}.")
                )
        if m.slug in seen_slugs:
            issues.append(
                PackIssue(severity="warning", mod=m.key, message=f"{m.title} looks like it's in the pack twice.")
            )
        seen_slugs.setdefault(m.slug, m)
        if not m.download_url:
            issues.append(
                PackIssue(
                    severity="warning",
                    mod=m.key,
                    message=f"{m.title}'s author blocks downloads outside the CurseForge app, so only the "
                    "CurseForge export includes it.",
                )
            )
        if m.server_side == "unsupported":
            issues.append(PackIssue(severity="info", mod=m.key, message=f"{m.title} is client-only; servers skip it."))
        elif m.client_side == "unsupported":
            issues.append(
                PackIssue(severity="info", mod=m.key, message=f"{m.title} is server-only; launchers skip it.")
            )
    return issues


def view(pack: CustomPack) -> PackView:
    return PackView(**pack.model_dump(), issues=check(pack))


async def retarget(pack: CustomPack) -> tuple[list[str], list[str]]:
    """Re-pick every mod for the pack's (new) Minecraft version and loader.

    The mods the user added are re-added from scratch so dependencies are worked
    out again. A mod with no fitting version is kept as it was and shows up as an
    error, so the user can decide what to do with it.
    """
    roots = [m for m in pack.mods if not m.added_as_dependency]
    pack.mods = []
    updated, unavailable = [], []
    for m in roots:
        if pack.find(m.key):
            continue  # already pulled in as another mod's dependency
        try:
            await add_mod(pack, AddModRequest(source=m.source, project_id=m.project_id))
        except ProviderError:
            pack.mods.append(m)
            unavailable.append(m.title)
            continue
        new = pack.find(m.key)
        if new and new.version_id != m.version_id:
            updated.append(m.title)
    # Mods the user added keep their "added by you" status even if another mod needs them.
    root_keys = {m.key for m in roots}
    for m in pack.mods:
        if m.key in root_keys:
            m.added_as_dependency = False
    return updated, unavailable
