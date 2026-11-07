"""Put a custom pack's server-side mods into a server's data folder.

Runs in the server provisioning thread, before the container starts. itzg then
installs the mod loader (TYPE=FABRIC/FORGE/...) and loads whatever is in /data/mods.
Jars this app put there are recorded in /data/.mcsm-pack.json, so re-installing a
changed pack removes mods that were taken out without touching mods the user
added by hand.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import httpx

from ..config import USER_AGENT
from ..models.pack import CustomPack, PackMod

log = logging.getLogger(__name__)
STATE_FILE = ".mcsm-pack.json"


def _known_bad(m: PackMod, loader: str):
    """Ask the shared known-bad mod list, when this build has it, whether to skip a mod."""
    try:
        from ..config import settings
        from .mod_db import get_mod_db  # added by the client-pack converter
    except ImportError:
        return None
    try:
        return get_mod_db(settings).check(mod_ids=[m.slug], filename=m.file_name, loader=loader)
    except Exception:  # never let the list break an install
        log.exception("Known-bad mod check failed for %s", m.title)
        return None


def server_mods(pack: CustomPack) -> tuple[list[PackMod], list[str]]:
    """The mods a server should get, and notes about the ones it skips."""
    keep, notes = [], []
    for m in pack.mods:
        if m.server_side == "unsupported":
            notes.append(f"Skipped {m.title}: client-only.")
            continue
        bad = _known_bad(m, pack.loader)
        if bad is not None:
            notes.append(f"Skipped {m.title}: {getattr(bad, 'reason', 'known to break servers')}.")
            continue
        keep.append(m)
    return keep, notes


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install(pack: CustomPack, data_dir: str, uid: int | None = None, gid: int | None = None) -> list[str]:
    """Download the pack's server mods into <data_dir>/mods. Returns notes for the user.

    Raises RuntimeError if a mod the server needs can't be downloaded.
    """
    root = Path(data_dir)
    mods_dir = root / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    state_path = root / STATE_FILE
    try:
        previous = set(json.loads(state_path.read_text()).get("files", []))
    except (FileNotFoundError, ValueError):
        previous = set()

    mods, notes = server_mods(pack)
    wanted = {}
    for m in mods:
        name = os.path.basename(m.file_name) or f"{m.slug}.jar"
        wanted[name] = m

    for name in previous - set(wanted):
        (mods_dir / name).unlink(missing_ok=True)

    failed = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120, follow_redirects=True) as client:
        for name, m in wanted.items():
            dest = mods_dir / name
            if dest.exists() and (not m.sha1 or _sha1(dest) == m.sha1.lower()):
                continue
            if not m.download_url:
                failed.append(f"{m.title} (its author only allows downloads in the CurseForge app)")
                continue
            tmp = dest.with_suffix(".part")
            try:
                h = hashlib.sha1()
                with client.stream("GET", m.download_url) as resp, tmp.open("wb") as out:
                    resp.raise_for_status()
                    for chunk in resp.iter_bytes():
                        h.update(chunk)
                        out.write(chunk)
                if m.sha1 and h.hexdigest() != m.sha1.lower():
                    raise RuntimeError("checksum mismatch")
                tmp.replace(dest)
            except (httpx.HTTPError, OSError, RuntimeError) as e:
                tmp.unlink(missing_ok=True)
                failed.append(f"{m.title} ({e})")
                continue
            if uid is not None:
                try:
                    os.chown(dest, uid, gid if gid is not None else -1)
                except OSError:
                    pass

    state_path.write_text(json.dumps({"pack": pack.id, "files": sorted(wanted)}, indent=2))
    if uid is not None:
        for p in (mods_dir, state_path):
            try:
                os.chown(p, uid, gid if gid is not None else -1)
            except OSError:
                pass
    if failed:
        raise RuntimeError("Could not download: " + "; ".join(failed))
    return notes
