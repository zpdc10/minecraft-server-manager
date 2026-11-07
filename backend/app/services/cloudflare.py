"""Give servers public addresses on the user's Cloudflare domain.

The app makes its own Cloudflare Tunnel ("minecraft-server-manager") and runs its own
cloudflared container for it, so it never edits a tunnel or connector the user already
has. Each server can get a hostname such as ``survival.example.com`` in one of two modes:

* ``tunnel``: a proxied CNAME to the tunnel plus an ingress rule
  ``tcp://localhost:<port>``. Nothing is opened on the router and the home IP stays
  hidden, but Cloudflare only carries raw TCP (Minecraft Java) to clients that run
  ``cloudflared access tcp`` on their own computer. Without Spectrum (a paid add-on),
  a player can't type the hostname straight into Minecraft.
* ``playit``: the server has a playit.gg tunnel (free, made in the playit dashboard,
  carried by the app's playit agent container). The app adds a DNS-only record and a
  ``_minecraft._tcp`` SRV record pointing at the tunnel's real host and port, so
  players type the hostname into Minecraft with nothing installed and no port forward.
* ``direct``: a DNS-only record pointing at the home's public IP plus a
  ``_minecraft._tcp`` SRV record with the server's port. Players type the hostname
  into Minecraft with nothing to install, but the port must be forwarded on the router
  and the home IP is visible.

DNS records the app makes carry a comment, and the app only ever deletes records it
made itself (it remembers their ids in cloudflare-routes.json).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

import httpx

from .. import settings_store
from ..config import USER_AGENT, settings
from . import playit

log = logging.getLogger(__name__)

API = os.environ.get("CLOUDFLARE_API_URL", "https://api.cloudflare.com/client/v4")
TRACE_URL = os.environ.get("CLOUDFLARE_TRACE_URL", "https://1.1.1.1/cdn-cgi/trace")
TUNNEL_NAME = "minecraft-server-manager"
# Not "mcsm-..." so it can never clash with a server's container name.
CONNECTOR_NAME = "mcsm_cloudflared"
CONNECTOR_IMAGE = os.environ.get("CLOUDFLARED_IMAGE", "cloudflare/cloudflared:latest")
COMPONENT_LABEL = "mcsm.component"
RECORD_COMMENT = "Managed by Minecraft Server Manager"
MODES = ("tunnel", "direct", "playit")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class CloudflareError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _routes_path() -> Path:
    return Path(settings.data_dir) / "cloudflare-routes.json"


def player_command(hostname: str) -> str:
    return f"cloudflared access tcp --hostname {hostname} --url 127.0.0.1:25565"


class Cloudflare:
    """Cloudflare API calls plus the app's saved Cloudflare settings and routes."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._transport = transport
        self._lock = threading.RLock()

    # ---- settings ---------------------------------------------------------

    def config(self) -> dict[str, Any]:
        return dict(settings_store.load().get("cloudflare") or {})

    def _save_config(self, **changes) -> dict[str, Any]:
        cfg = self.config()
        for k, v in changes.items():
            if v is None:
                cfg.pop(k, None)
            else:
                cfg[k] = v
        settings_store.save({"cloudflare": cfg or None})
        return cfg

    def token(self) -> str:
        return (self.config().get("api_token") or os.environ.get("CLOUDFLARE_API_TOKEN", "")).strip()

    def is_ready(self) -> bool:
        cfg = self.config()
        return bool(self.token() and cfg.get("zone_id") and cfg.get("tunnel_id"))

    def routes(self) -> dict[str, dict]:
        try:
            return json.loads(_routes_path().read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_routes(self, routes: dict[str, dict]) -> None:
        path = _routes_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(routes, indent=2))
        tmp.replace(path)

    # ---- HTTP -------------------------------------------------------------

    def _http(self) -> httpx.Client:
        return httpx.Client(
            timeout=20,
            headers={"User-Agent": USER_AGENT},
            transport=self._transport,
        )

    def api(self, method: str, path: str, token: str | None = None, **kwargs) -> Any:
        token = token or self.token()
        if not token:
            raise CloudflareError("Add a Cloudflare API token first.")
        try:
            with self._http() as http:
                resp = http.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        except httpx.HTTPError as e:
            raise CloudflareError(f"Could not reach Cloudflare: {e}", 502) from e
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or not body.get("success", False):
            errors = body.get("errors") or []
            msg = "; ".join(str(e.get("message", e)) for e in errors) or f"HTTP {resp.status_code}"
            status = 400 if resp.status_code < 500 else 502
            if resp.status_code in (401, 403):
                msg = f"Cloudflare refused the request ({msg}). Check the token's permissions."
            err = CloudflareError(f"Cloudflare: {msg}", status)
            err.cf_status = resp.status_code  # type: ignore[attr-defined]
            raise err
        return body.get("result")

    def public_ip(self) -> str:
        try:
            with self._http() as http:
                text = http.get(TRACE_URL).text
        except httpx.HTTPError as e:
            raise CloudflareError(f"Could not find this network's public IP: {e}", 502) from e
        for line in text.splitlines():
            if line.startswith("ip="):
                return line[3:].strip()
        raise CloudflareError("Could not find this network's public IP.", 502)

    # ---- account and zone -------------------------------------------------

    def zones(self, token: str | None = None) -> list[dict]:
        result = self.api("GET", "/zones", token=token, params={"per_page": 50, "status": "active"})
        return [
            {
                "id": z["id"],
                "name": z["name"],
                "account_id": (z.get("account") or {}).get("id", ""),
                "account_name": (z.get("account") or {}).get("name", ""),
            }
            for z in result or []
        ]

    def save_token(self, token: str) -> list[dict]:
        token = token.strip()
        zones = self.zones(token=token)
        if not zones:
            raise CloudflareError(
                "That token can't see any domains. Give it Zone > Zone > Read and Zone > DNS > Edit for your domain."
            )
        with self._lock:
            self._save_config(api_token=token)
        return zones

    def configure(
        self, zone_id: str, auto_route: bool, default_mode: str, direct_target: str, manager
    ) -> dict[str, Any]:
        if default_mode not in MODES:
            raise CloudflareError(f"Mode must be one of {', '.join(MODES)}")
        direct_target = direct_target.strip().rstrip(".")
        zone = next((z for z in self.zones() if z["id"] == zone_id), None)
        if zone is None:
            raise CloudflareError("That domain isn't available to this token.")
        with self._lock:
            cfg = self.config()
            if cfg.get("zone_id") and cfg["zone_id"] != zone_id and self.routes():
                raise CloudflareError("Remove the servers' public addresses before switching to another domain.", 409)
            self._save_config(
                zone_id=zone["id"],
                zone_name=zone["name"],
                account_id=zone["account_id"],
                auto_route=auto_route,
                default_mode=default_mode,
                direct_target=direct_target or None,
            )
            self.ensure_tunnel()
            self.ensure_connector(manager)
        return self.config()

    # ---- playit agent ------------------------------------------------------

    def save_playit_key(self, secret_key: str, manager) -> None:
        """Run the playit agent with this key, or remove it when the key is empty."""
        secret_key = secret_key.strip()
        with self._lock:
            if not secret_key:
                playit.remove_agent(manager)
                self._save_config(playit_secret=None)
                return
            playit.ensure_agent(manager, secret_key)
            self._save_config(playit_secret=secret_key)

    # ---- tunnel and connector ---------------------------------------------

    def ensure_tunnel(self) -> str:
        cfg = self.config()
        acct = cfg.get("account_id")
        if not acct:
            raise CloudflareError("Choose a domain first.")
        tid = cfg.get("tunnel_id")
        if tid:
            try:
                t = self.api("GET", f"/accounts/{acct}/cfd_tunnel/{tid}")
                if t and not t.get("deleted_at"):
                    return tid
            except CloudflareError as e:
                if getattr(e, "cf_status", 0) != 404:
                    raise
        found = self.api("GET", f"/accounts/{acct}/cfd_tunnel", params={"name": TUNNEL_NAME, "is_deleted": "false"})
        if found:
            tid = found[0]["id"]
        else:
            created = self.api(
                "POST", f"/accounts/{acct}/cfd_tunnel", json={"name": TUNNEL_NAME, "config_src": "cloudflare"}
            )
            tid = created["id"]
        self._save_config(tunnel_id=tid)
        self.sync_ingress()
        return tid

    def tunnel_status(self) -> dict[str, Any]:
        cfg = self.config()
        if not (cfg.get("account_id") and cfg.get("tunnel_id")):
            return {"status": "none", "connections": 0}
        try:
            t = self.api("GET", f"/accounts/{cfg['account_id']}/cfd_tunnel/{cfg['tunnel_id']}")
        except CloudflareError as e:
            return {"status": "error", "connections": 0, "error": e.message}
        return {"status": t.get("status", "unknown"), "connections": len(t.get("connections") or [])}

    def ensure_connector(self, manager) -> None:
        """Run (or update) the cloudflared container for the app's tunnel."""
        from docker.errors import ImageNotFound, NotFound

        cfg = self.config()
        token = self.api("GET", f"/accounts/{cfg['account_id']}/cfd_tunnel/{cfg['tunnel_id']}/token")
        client = manager.client
        try:
            c = client.containers.get(CONNECTOR_NAME)
        except NotFound:
            c = None
        if c is not None:
            env = dict(e.split("=", 1) for e in (c.attrs.get("Config", {}).get("Env") or []) if "=" in e)
            if env.get("TUNNEL_TOKEN") == token:
                if c.status != "running":
                    c.start()
                return
            c.remove(force=True)
        try:
            client.images.get(CONNECTOR_IMAGE)
        except ImageNotFound:
            repo, _, tag = CONNECTOR_IMAGE.rpartition(":")
            client.images.pull(repo or CONNECTOR_IMAGE, tag=tag if repo else "latest")
        c = client.containers.create(
            CONNECTOR_IMAGE,
            name=CONNECTOR_NAME,
            command=["tunnel", "--no-autoupdate", "run"],
            environment={"TUNNEL_TOKEN": token},
            # Host networking lets the tunnel reach every server at localhost:<port>,
            # whatever Docker network the server is on.
            network_mode="host",
            restart_policy={"Name": "unless-stopped"},
            labels={COMPONENT_LABEL: "cloudflared"},
            detach=True,
        )
        c.start()

    def connector_status(self, manager) -> str:
        from docker.errors import DockerException, NotFound

        try:
            return manager.client.containers.get(CONNECTOR_NAME).status
        except NotFound:
            return "missing"
        except DockerException:
            return "unknown"

    def remove_connector(self, manager) -> None:
        from docker.errors import NotFound

        try:
            manager.client.containers.get(CONNECTOR_NAME).remove(force=True)
        except NotFound:
            pass

    def sync_ingress(self, ports: dict[str, int] | None = None) -> None:
        """Rewrite the app's tunnel config from the saved routes. Only the app's own tunnel."""
        cfg = self.config()
        if not (cfg.get("account_id") and cfg.get("tunnel_id")):
            return
        routes = self.routes()
        ingress = []
        for sid, r in sorted(routes.items()):
            port = (ports or {}).get(sid) or r.get("port")
            if r.get("mode") == "tunnel" and port:
                ingress.append({"hostname": r["hostname"], "service": f"tcp://localhost:{port}"})
        ingress.append({"service": "http_status:404"})
        self.api(
            "PUT",
            f"/accounts/{cfg['account_id']}/cfd_tunnel/{cfg['tunnel_id']}/configurations",
            json={"config": {"ingress": ingress}},
        )

    # ---- DNS --------------------------------------------------------------

    def _zone(self) -> tuple[str, str]:
        cfg = self.config()
        if not cfg.get("zone_id"):
            raise CloudflareError("Choose a domain on the Settings page first.")
        return cfg["zone_id"], cfg["zone_name"]

    def _create_record(self, record: dict) -> str:
        zone_id, _ = self._zone()
        result = self.api("POST", f"/zones/{zone_id}/dns_records", json={**record, "comment": RECORD_COMMENT})
        return result["id"]

    def _delete_record(self, record_id: str) -> None:
        zone_id, _ = self._zone()
        try:
            self.api("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
        except CloudflareError as e:
            if getattr(e, "cf_status", 0) != 404:
                raise

    def _existing(self, name: str) -> list[dict]:
        zone_id, _ = self._zone()
        return self.api("GET", f"/zones/{zone_id}/dns_records", params={"name": name}) or []

    def _direct_target(self) -> tuple[str, str]:
        """(record type, content) for direct mode: the saved target, or the detected public IP."""
        target = self.config().get("direct_target") or self.public_ip()
        try:
            ip = ipaddress.ip_address(target)
        except ValueError:
            return "CNAME", target
        return ("AAAA" if ip.version == 6 else "A"), str(ip)

    def _make_records(self, hostname: str, mode: str, port: int, target: tuple[str, int] | None = None) -> list[dict]:
        cfg = self.config()
        if mode == "playit":
            host, tport = target or ("", 0)
            try:
                ip = ipaddress.ip_address(host)
                main = {"type": "AAAA" if ip.version == 6 else "A", "content": str(ip)}
                srv_target = hostname
            except ValueError:
                main = {"type": "CNAME", "content": host}
                srv_target = host
            return [
                {**main, "name": hostname, "proxied": False, "ttl": 300},
                {
                    "type": "SRV",
                    "name": f"_minecraft._tcp.{hostname}",
                    "data": {"priority": 0, "weight": 5, "port": tport, "target": srv_target},
                    "ttl": 300,
                },
            ]
        if mode == "tunnel":
            return [
                {
                    "type": "CNAME",
                    "name": hostname,
                    "content": f"{cfg['tunnel_id']}.cfargotunnel.com",
                    "proxied": True,
                }
            ]
        rtype, content = self._direct_target()
        return [
            {"type": rtype, "name": hostname, "content": content, "proxied": False, "ttl": 300},
            {
                "type": "SRV",
                "name": f"_minecraft._tcp.{hostname}",
                "data": {"priority": 0, "weight": 5, "port": port, "target": hostname},
                "ttl": 300,
            },
        ]

    # ---- routes -----------------------------------------------------------

    def add_route(
        self, server_id: str, port: int, subdomain: str = "", mode: str = "", playit_address: str = ""
    ) -> dict:
        cfg = self.config()
        if not self.is_ready():
            # Without Cloudflare, playit still works: players use playit's own address.
            if (mode or "playit") != "playit":
                raise CloudflareError("Connect Cloudflare on the Settings page first.")
            return self._add_playit_only(server_id, port, playit_address)
        mode = mode or cfg.get("default_mode") or "tunnel"
        if mode not in MODES:
            raise CloudflareError(f"Mode must be one of {', '.join(MODES)}")
        label = (subdomain or server_id).strip().lower()
        if not _LABEL_RE.match(label):
            raise CloudflareError(
                "Use one word of letters, numbers and dashes for the address, like 'survival'. "
                "Cloudflare's free certificate only covers one level below your domain."
            )
        _, zone_name = self._zone()
        hostname = f"{label}.{zone_name}"
        target = None
        if mode == "playit":
            old_addr = (self.routes().get(server_id) or {}).get("playit_address", "")
            playit_address = (playit_address or old_addr).strip()
            if not playit_address:
                raise CloudflareError("Paste the server's playit.gg address (from the playit dashboard).")
            try:
                target = playit.resolve(playit_address)
            except playit.PlayitError as e:
                raise CloudflareError(str(e)) from e
            if target[0] == hostname:
                raise CloudflareError("Use the address playit gave the tunnel, not your own domain.")
        with self._lock:
            routes = self.routes()
            old = routes.get(server_id)
            for sid, r in routes.items():
                if sid != server_id and r["hostname"] == hostname:
                    raise CloudflareError(f"{hostname} is already used by another server.", 409)
            ours = set((old or {}).get("record_ids") or [])
            names = {hostname, f"_minecraft._tcp.{hostname}"}
            clash = [r for n in names for r in self._existing(n) if r["id"] not in ours]
            if clash:
                raise CloudflareError(
                    f"{hostname} already has DNS records the app didn't make. Pick another name, "
                    "or delete them in Cloudflare first.",
                    409,
                )
            if old:
                self._remove_records(old)
            created: list[str] = []
            try:
                for rec in self._make_records(hostname, mode, port, target):
                    created.append(self._create_record(rec))
            except CloudflareError:
                for rid in created:
                    self._delete_record(rid)
                if old:
                    routes.pop(server_id, None)
                    self._save_routes(routes)
                raise
            route = {"hostname": hostname, "mode": mode, "port": port, "record_ids": created}
            if target:
                route.update(playit_address=playit_address, target=list(target))
            routes[server_id] = route
            self._save_routes(routes)
            if mode == "tunnel" or (old and old.get("mode") == "tunnel"):
                self.sync_ingress()
        return route

    def _add_playit_only(self, server_id: str, port: int, playit_address: str) -> dict:
        """A playit route with no DNS of our own: players join playit's address directly."""
        old_addr = (self.routes().get(server_id) or {}).get("playit_address", "")
        playit_address = (playit_address or old_addr).strip()
        if not playit_address:
            raise CloudflareError("Paste the server's playit.gg address (from the playit dashboard).")
        try:
            host, addr_port = playit.parse_address(playit_address)
        except playit.PlayitError as e:
            raise CloudflareError(str(e)) from e
        with self._lock:
            routes = self.routes()
            old = routes.get(server_id)
            if old and old.get("record_ids"):
                self._remove_records(old)
            hostname = f"{host}:{addr_port}" if addr_port else host
            route = {
                "hostname": hostname,
                "mode": "playit",
                "port": port,
                "record_ids": [],
                "playit_address": playit_address,
            }
            routes[server_id] = route
            self._save_routes(routes)
        return route

    def _remove_records(self, route: dict) -> None:
        for rid in route.get("record_ids") or []:
            self._delete_record(rid)

    def remove_route(self, server_id: str) -> bool:
        with self._lock:
            routes = self.routes()
            route = routes.get(server_id)
            if route is None:
                return False
            self._remove_records(route)
            routes.pop(server_id)
            self._save_routes(routes)
            if route.get("mode") == "tunnel":
                self.sync_ingress()
        return True

    def sync(self, ports: dict[str, int]) -> None:
        """Bring Cloudflare in line with the servers' current ports and the public IP."""
        if not self.is_ready():
            return
        with self._lock:
            routes = self.routes()
            changed = False
            for sid, r in routes.items():
                port = ports.get(sid)
                if port and port != r.get("port"):
                    r["port"] = port
                    changed = True
                    if r["mode"] == "direct":
                        self._rebuild_direct(r)
            if any(r["mode"] == "direct" for r in routes.values()):
                self._refresh_direct_target(routes)
            for r in routes.values():
                if r["mode"] == "playit" and r.get("playit_address"):
                    changed |= self._refresh_playit(r)
            if changed:
                self._save_routes(routes)
            self.sync_ingress(ports)

    def _refresh_playit(self, route: dict) -> bool:
        """Re-point a playit route's records if playit moved the tunnel to a new host or port."""
        try:
            target = list(playit.resolve(route["playit_address"]))
        except playit.PlayitError as e:
            log.warning("Could not look up %s: %s", route["playit_address"], e)
            return False
        if target == route.get("target"):
            return False
        self._remove_records(route)
        records = self._make_records(route["hostname"], "playit", route["port"], tuple(target))
        route["record_ids"] = [self._create_record(r) for r in records]
        route["target"] = target
        return True

    def _rebuild_direct(self, route: dict) -> None:
        self._remove_records(route)
        records = self._make_records(route["hostname"], "direct", route["port"])
        route["record_ids"] = [self._create_record(r) for r in records]

    def _refresh_direct_target(self, routes: dict[str, dict]) -> None:
        """Point direct-mode records at the current public IP if it changed."""
        zone_id, _ = self._zone()
        rtype, content = self._direct_target()
        for r in routes.values():
            if r["mode"] != "direct" or not r.get("record_ids"):
                continue
            main_id = r["record_ids"][0]
            try:
                rec = self.api("GET", f"/zones/{zone_id}/dns_records/{main_id}")
            except CloudflareError:
                continue
            if rec.get("content") != content or rec.get("type") != rtype:
                self.api(
                    "PATCH",
                    f"/zones/{zone_id}/dns_records/{main_id}",
                    json={"type": rtype, "content": content, "name": r["hostname"], "proxied": False},
                )

    def disconnect(self, manager) -> None:
        """Remove every address the app made, the app's tunnel and connector, and the token."""
        with self._lock:
            cfg = self.config()
            if self.token() and cfg.get("zone_id"):
                for sid in list(self.routes()):
                    route = self.routes()[sid]
                    self._remove_records(route)
                    routes = self.routes()
                    routes.pop(sid, None)
                    if route.get("mode") == "playit" and route.get("playit_address"):
                        # The playit tunnel still works; players go back to playit's own address.
                        host, port = playit.parse_address(route["playit_address"])
                        routes[sid] = {
                            "hostname": f"{host}:{port}" if port else host,
                            "mode": "playit",
                            "port": route.get("port", 0),
                            "record_ids": [],
                            "playit_address": route["playit_address"],
                        }
                    self._save_routes(routes)
            self.remove_connector(manager)
            if self.token() and cfg.get("account_id") and cfg.get("tunnel_id"):
                acct, tid = cfg["account_id"], cfg["tunnel_id"]
                try:
                    self.api("DELETE", f"/accounts/{acct}/cfd_tunnel/{tid}/connections")
                except CloudflareError:
                    pass
                try:
                    self.api("DELETE", f"/accounts/{acct}/cfd_tunnel/{tid}")
                except CloudflareError as e:
                    if getattr(e, "cf_status", 0) != 404:
                        raise
            # The playit agent doesn't depend on Cloudflare, so keep its key.
            secret = cfg.get("playit_secret")
            settings_store.save({"cloudflare": {"playit_secret": secret} if secret else None})


_instance: Cloudflare | None = None


def get_cloudflare() -> Cloudflare:
    global _instance
    if _instance is None:
        _instance = Cloudflare()
    return _instance


class SyncLoop:
    """Every few minutes, update tunnel ports and the public IP for direct-mode records."""

    def __init__(self, manager_factory, interval: float = 600):
        self._manager_factory = manager_factory
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="cloudflare-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            cf = get_cloudflare()
            if not cf.is_ready() or not cf.routes():
                continue
            try:
                ports = {s.id: s.port for s in self._manager_factory().list_servers()}
                cf.sync(ports)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("Cloudflare sync failed")
