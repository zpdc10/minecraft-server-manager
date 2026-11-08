"""Interactive shell (websocket) and pack mode switching for one server."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, WebSocket
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ..services.docker_manager import ServerConflict, ServerNotFound
from ..services.pack_modes import PackModeError, PackModes
from ..services.shell import ShellError, open_shell, request_allowed
from .backups import get_backups
from .servers import Manager, _call, get_manager

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/servers/{server_id}", tags=["shell"])


class PackModeRequest(BaseModel):
    mode: str


def _modes(mgr) -> PackModes:
    return PackModes(mgr, get_backups())


@router.get("/pack-mode")
def pack_mode(server_id: str, mgr: Manager):
    return _call(_modes(mgr).info, server_id)


@router.post("/pack-mode")
def switch_pack_mode(server_id: str, req: PackModeRequest, mgr: Manager):
    try:
        _call(_modes(mgr).start_switch, server_id, req.mode)
    except PackModeError as e:
        raise HTTPException(400, str(e)) from e
    return _call(_modes(mgr).info, server_id)


@router.post("/shell/prepare")
def prepare(server_id: str, mgr: Manager):
    """Stop the server and take a full backup, before running a script that changes files."""
    info = _call(mgr.get_server, server_id)
    stopped = False
    if info.status in ("running", "starting"):
        _call(mgr.stop_server, server_id)
        stopped = True
    try:
        backup = get_backups().take_backup(server_id, "pre-script", "full")
    except Exception as e:
        raise HTTPException(500, f"Backup failed, so nothing was run: {e}") from e
    return {"stopped": stopped, "backup_id": backup.id}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@router.websocket("/shell")
async def shell(ws: WebSocket, server_id: str, root: bool = False, cols: int = 80, rows: int = 24):
    """Binary frames from the server are terminal output. The client sends JSON text frames:
    {"t": "i", "d": "<input>"} and {"t": "r", "c": cols, "r": rows}."""
    ok, reason = request_allowed(ws.client.host if ws.client else None, ws.headers)
    if not ok:
        log.warning("Refused shell for %s from %s: %s", server_id, ws.client, reason)
        await ws.close(code=4403, reason=reason)
        return
    await ws.accept()
    mgr = ws.app.dependency_overrides.get(get_manager, get_manager)()
    try:
        session = await run_in_threadpool(
            open_shell, mgr, server_id, root, _clamp(cols, 10, 500), _clamp(rows, 5, 200)
        )
    except (ShellError, ServerConflict) as e:
        await ws.send_text(json.dumps({"t": "error", "d": str(e)}))
        await ws.close()
        return
    except ServerNotFound:
        await ws.send_text(json.dumps({"t": "error", "d": f"Server '{server_id}' not found"}))
        await ws.close()
        return

    if session.note:
        await ws.send_text(json.dumps({"t": "note", "d": session.note}))
    loop = asyncio.get_running_loop()

    async def pump_out():
        while True:
            data = await loop.run_in_executor(None, session.read)
            if not data:
                return
            await ws.send_bytes(data)

    async def pump_in():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes"):
                await loop.run_in_executor(None, session.write, msg["bytes"])
                continue
            try:
                frame = json.loads(msg.get("text") or "{}")
            except ValueError:
                continue
            if frame.get("t") == "i" and isinstance(frame.get("d"), str):
                await loop.run_in_executor(None, session.write, frame["d"].encode())
            elif frame.get("t") == "r":
                try:
                    c, r = _clamp(int(frame["c"]), 10, 500), _clamp(int(frame["r"]), 5, 200)
                except (KeyError, TypeError, ValueError):
                    continue
                await loop.run_in_executor(None, session.resize, c, r)

    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
    out_finished, code = False, None
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            t.exception()  # retrieved so asyncio doesn't log it; a closed socket is expected
        out_finished = tasks[0] in done
        if out_finished:
            code = await run_in_threadpool(session.exit_code)
    finally:
        await run_in_threadpool(session.close)
    if out_finished:
        try:
            await ws.send_text(json.dumps({"t": "exit", "code": code}))
            await ws.close()
        except Exception:  # noqa: BLE001  (client already gone)
            pass
