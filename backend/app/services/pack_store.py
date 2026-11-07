"""Custom packs stored as one JSON file each under <DATA_DIR>/packs."""

import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from ..config import settings
from ..models.pack import CustomPack

_lock = threading.Lock()
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class PackNotFound(Exception):
    pass


def _dir() -> Path:
    return Path(settings.data_dir) / "packs"


def _path(pack_id: str) -> Path:
    if not _ID.match(pack_id):
        raise PackNotFound(pack_id)
    return _dir() / f"{pack_id}.json"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "pack"
    candidate, n = base, 2
    while _path(candidate).exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def list_packs() -> list[CustomPack]:
    packs = []
    for f in sorted(_dir().glob("*.json")) if _dir().is_dir() else []:
        try:
            packs.append(CustomPack.model_validate_json(f.read_text()))
        except ValueError:
            continue  # skip a corrupt file rather than breaking the whole list
    return sorted(packs, key=lambda p: p.updated_at, reverse=True)


def get(pack_id: str) -> CustomPack:
    try:
        return CustomPack.model_validate_json(_path(pack_id).read_text())
    except FileNotFoundError as e:
        raise PackNotFound(pack_id) from e


def save(pack: CustomPack) -> CustomPack:
    with _lock:
        pack.updated_at = now()
        if not pack.created_at:
            pack.created_at = pack.updated_at
        path = _path(pack.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(pack.model_dump(), indent=2))
        os.replace(tmp, path)
        return pack


def delete(pack_id: str) -> None:
    try:
        _path(pack_id).unlink()
    except FileNotFoundError as e:
        raise PackNotFound(pack_id) from e
