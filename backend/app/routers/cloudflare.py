"""Link the app to a Cloudflare account and give servers public addresses."""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.cloudflare import CloudflareError, get_cloudflare, player_command
from .servers import Manager, _call

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cloudflare", tags=["cloudflare"])

Mode = Literal["tunnel", "direct"]


class Zone(BaseModel):
    id: str
    name: str
    account_id: str = ""
    account_name: str = ""


class CloudflareStatus(BaseModel):
    token_set: bool
    token_hint: str | None = None
    zone_id: str = ""
    zone_name: str = ""
    tunnel_id: str = ""
    # Cloudflare's view of the tunnel: healthy | inactive | down | degraded | none | error
    tunnel_status: str = "none"
    tunnel_connections: int = 0
    # The app's cloudflared container: running | exited | missing | ...
    connector_status: str = "missing"
    auto_route: bool = False
    default_mode: Mode = "tunnel"
    direct_target: str = ""
    error: str = ""


class TokenUpdate(BaseModel):
    api_token: str


class ConfigUpdate(BaseModel):
    zone_id: str
    auto_route: bool = True
    default_mode: Mode = "tunnel"
    # Public IP or hostname for direct mode. Empty means detect the public IP.
    direct_target: str = ""


class Route(BaseModel):
    server_id: str
    hostname: str
    mode: Mode
    port: int
    # What players type into Minecraft.
    connect_address: str
    # Tunnel mode: the command each player runs first.
    player_command: str = ""


class RouteUpdate(BaseModel):
    subdomain: str = ""
    mode: Mode | None = None


def _cf_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except CloudflareError as e:
        raise HTTPException(e.status_code, e.message) from e


def _status(mgr) -> CloudflareStatus:
    cf = get_cloudflare()
    cfg = cf.config()
    token = cf.token()
    status = CloudflareStatus(
        token_set=bool(token),
        token_hint=f"…{token[-4:]}" if len(token) >= 8 else None,
        zone_id=cfg.get("zone_id", ""),
        zone_name=cfg.get("zone_name", ""),
        tunnel_id=cfg.get("tunnel_id", ""),
        auto_route=bool(cfg.get("auto_route")),
        default_mode=cfg.get("default_mode") or "tunnel",
        direct_target=cfg.get("direct_target", ""),
    )
    if cfg.get("tunnel_id"):
        t = cf.tunnel_status()
        status.tunnel_status = t["status"]
        status.tunnel_connections = t["connections"]
        status.error = t.get("error", "")
        status.connector_status = cf.connector_status(mgr)
    return status


def _route(server_id: str, r: dict) -> Route:
    tunnel = r["mode"] == "tunnel"
    return Route(
        server_id=server_id,
        hostname=r["hostname"],
        mode=r["mode"],
        port=r.get("port") or 0,
        connect_address="127.0.0.1:25565" if tunnel else r["hostname"],
        player_command=player_command(r["hostname"]) if tunnel else "",
    )


@router.get("", response_model=CloudflareStatus)
def get_status(mgr: Manager):
    return _status(mgr)


@router.put("/token", response_model=list[Zone])
def save_token(body: TokenUpdate):
    if not body.api_token.strip():
        raise HTTPException(400, "Paste an API token.")
    return _cf_call(get_cloudflare().save_token, body.api_token)


@router.get("/zones", response_model=list[Zone])
def zones():
    return _cf_call(get_cloudflare().zones)


@router.put("/config", response_model=CloudflareStatus)
def configure(body: ConfigUpdate, mgr: Manager):
    cf = get_cloudflare()
    _cf_call(_call, cf.configure, body.zone_id, body.auto_route, body.default_mode, body.direct_target, mgr)
    return _status(mgr)


@router.post("/sync", response_model=CloudflareStatus)
def sync(mgr: Manager):
    cf = get_cloudflare()
    if cf.is_ready():
        _cf_call(_call, cf.ensure_connector, mgr)
        ports = {s.id: s.port for s in _call(mgr.list_servers)}
        _cf_call(cf.sync, ports)
    return _status(mgr)


@router.delete("", status_code=204)
def disconnect(mgr: Manager):
    _cf_call(_call, get_cloudflare().disconnect, mgr)


@router.get("/routes", response_model=list[Route])
def list_routes():
    return [_route(sid, r) for sid, r in sorted(get_cloudflare().routes().items())]


@router.put("/routes/{server_id}", response_model=Route)
def set_route(server_id: str, body: RouteUpdate, mgr: Manager):
    info = _call(mgr.get_server, server_id)
    cf = get_cloudflare()
    r = _cf_call(cf.add_route, server_id, info.port, body.subdomain, body.mode or "")
    return _route(server_id, r)


@router.delete("/routes/{server_id}", status_code=204)
def delete_route(server_id: str):
    if not _cf_call(get_cloudflare().remove_route, server_id):
        raise HTTPException(404, "This server has no public address.")


# ---- hooks used by the servers routers --------------------------------------


def on_server_created(server_id: str, port: int) -> None:
    """Give a new server a public address if the user turned that on. Never fails the create."""
    cf = get_cloudflare()
    if not (cf.is_ready() and cf.config().get("auto_route")):
        return
    try:
        cf.add_route(server_id, port)
    except CloudflareError as e:
        log.warning("Could not make a public address for %s: %s", server_id, e.message)


def on_server_deleted(server_id: str) -> None:
    cf = get_cloudflare()
    if server_id not in cf.routes():
        return
    try:
        cf.remove_route(server_id)
    except CloudflareError as e:
        log.warning("Could not remove the public address of %s: %s", server_id, e.message)


def on_server_changed(server_id: str, port: int) -> None:
    cf = get_cloudflare()
    route = cf.routes().get(server_id)
    if route is None or route.get("port") == port:
        return
    try:
        cf.sync({server_id: port})
    except CloudflareError as e:
        log.warning("Could not update the public address of %s: %s", server_id, e.message)
