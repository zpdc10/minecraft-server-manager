"""The app's playit.gg agent, and resolving playit addresses for Cloudflare DNS.

playit.gg carries raw TCP for free (up to 4 tunnels), so players join with nothing
installed and nothing is opened on the home router. The agent container here keeps
an outgoing connection to playit; tunnels are made in the playit dashboard, each
pointing at 127.0.0.1:<server port>.

To give a tunnel an address on the user's own domain, the app adds a
``_minecraft._tcp`` SRV record pointing at the tunnel's real host and port. playit's
free ``*.joinmc.link`` names are themselves SRV records, and Minecraft only follows
one SRV hop, so the app looks up the real target first.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re

import httpx

from ..config import USER_AGENT

log = logging.getLogger(__name__)

CONTAINER_NAME = "mcsm_playit"
IMAGE = os.environ.get("PLAYIT_IMAGE", "playitcloud/playit:0.15.19")
COMPONENT_LABEL = "mcsm.component"
DOH_URL = os.environ.get("DOH_URL", "https://cloudflare-dns.com/dns-query")
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$", re.I)


class PlayitError(Exception):
    pass


def parse_address(address: str) -> tuple[str, int | None]:
    """'abc.joinmc.link', 'host.ply.gg:12345' or '1.2.3.4:12345' -> (host, port or None)."""
    text = address.strip().lower()
    text = re.sub(r"^[a-z]+://", "", text).rstrip("/").rstrip(".")
    host, port = text, None
    if text.count(":") == 1:
        host, _, p = text.partition(":")
        if not p.isdigit() or not 0 < int(p) < 65536:
            raise PlayitError("The port after ':' must be a number from 1 to 65535.")
        port = int(p)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOST_RE.match(host):
            raise PlayitError(f"'{address}' doesn't look like a playit address.") from None
    return host, port


def _doh(name: str, rtype: str) -> list[dict]:
    with httpx.Client(timeout=10, headers={"User-Agent": USER_AGENT, "Accept": "application/dns-json"}) as http:
        resp = http.get(DOH_URL, params={"name": name, "type": rtype})
        resp.raise_for_status()
        return resp.json().get("Answer") or []


def resolve(address: str) -> tuple[str, int]:
    """The host and port Minecraft really connects to for a playit address."""
    host, port = parse_address(address)
    if port is not None:
        return host, port
    try:
        answers = [a for a in _doh(f"_minecraft._tcp.{host}", "SRV") if a.get("type") == 33]
    except (httpx.HTTPError, ValueError) as e:
        raise PlayitError(f"Couldn't look up {host}: {e}") from e
    if answers:
        # "priority weight port target."
        parts = str(answers[0].get("data", "")).split()
        if len(parts) == 4 and parts[2].isdigit():
            return parts[3].rstrip("."), int(parts[2])
    return host, 25565


def ensure_agent(manager, secret_key: str) -> None:
    """Run (or update) the playit agent container with this secret key."""
    from docker.errors import ImageNotFound, NotFound

    client = manager.client
    try:
        c = client.containers.get(CONTAINER_NAME)
    except NotFound:
        c = None
    if c is not None:
        env = dict(e.split("=", 1) for e in (c.attrs.get("Config", {}).get("Env") or []) if "=" in e)
        if env.get("SECRET_KEY") == secret_key:
            if c.status != "running":
                c.start()
            return
        c.remove(force=True)
    try:
        client.images.get(IMAGE)
    except ImageNotFound:
        repo, _, tag = IMAGE.rpartition(":")
        client.images.pull(repo or IMAGE, tag=tag if repo else "latest")
    c = client.containers.create(
        IMAGE,
        name=CONTAINER_NAME,
        environment={"SECRET_KEY": secret_key},
        # Host networking lets the agent reach every server at 127.0.0.1:<port>.
        network_mode="host",
        restart_policy={"Name": "unless-stopped"},
        labels={COMPONENT_LABEL: "playit"},
        detach=True,
    )
    c.start()


def agent_status(manager) -> str:
    from docker.errors import DockerException, NotFound

    try:
        return manager.client.containers.get(CONTAINER_NAME).status
    except NotFound:
        return "missing"
    except DockerException:
        return "unknown"


def remove_agent(manager) -> None:
    from docker.errors import NotFound

    try:
        manager.client.containers.get(CONTAINER_NAME).remove(force=True)
    except NotFound:
        pass
