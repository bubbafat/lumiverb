"""Whether other devices on the network may use a Mac's Lumiverb: its own
nginx listens on this machine only (as a new install does: it's set up
there first), or for other devices too, over HTTPS alone.

This machine's own browser keeps plain HTTP on WEB_PORT, on localhost. Other
devices get HTTPS on a port of its own (WEB_TLS_PORT, else WEB_PORT + 1),
with the certificate tls.py serves (Lumiverb's own authority's, or one an
admin gave); one that comes to WEB_PORT is sent there. The authority's
certificate, for a device to trust, is at /lumiverb-ca.crt.

The choice is an admin's (Settings › Network: routers/system.py), kept in
DATA_DIR/network.json. `lumiverb-server run` owns nginx, so it applies it:
it writes the lines nginx's config includes in its server (DATA_DIR/nginx/
listen.conf), at start and whenever the choice or the certificate changes,
and restarts nginx. An nginx set up some other way (a Linux install's, by
deploy-web.sh) isn't this install's to change: it's "not available" here.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger("lumiverb-server")

NETWORK_FILE = "network.json"
LISTEN_FILE = Path("nginx") / "listen.conf"
WATCH_EVERY_SEC = 2.0


def listen_file(data_dir: Path) -> Path:
    return data_dir / LISTEN_FILE


def include_line(data_dir: Path) -> str:
    """What nginx's config says to use the listen lines (init.py writes it)."""
    return f"include {listen_file(data_dir)};"


def other_devices(data_dir: Path) -> bool:
    """The admin's choice; this machine only until they make one."""
    try:
        return bool(json.loads((data_dir / NETWORK_FILE).read_text()).get("other_devices", False))
    except (OSError, ValueError, AttributeError):
        return False


def set_other_devices(data_dir: Path, on: bool) -> None:
    path = data_dir / NETWORK_FILE
    temporary = path.with_name(f".{NETWORK_FILE}.new")
    temporary.write_text(json.dumps({"other_devices": on}) + "\n")
    os.replace(temporary, path)


def tls_port(web_port: int, setting: str | int | None = None) -> int:
    """Where other devices reach it over HTTPS: WEB_TLS_PORT, else WEB_PORT + 1."""
    return int(setting) if setting not in (None, "", 0, "0") else web_port + 1


def listen_lines(port: int, other_devices: bool, *, tls_port: int | None = None, served=None,
                 ca: Path | None = None) -> str:
    """nginx's lines for the choice, in its server block: where it listens,
    the certificate it serves other devices (`served`, tls.Served), and the
    authority's certificate for them to trust (`ca`)."""
    said = "# lumiverb-server run's, from Settings › Network: rewritten when that changes.\n"
    trust = ("location = /lumiverb-ca.crt {\n"
             f"    alias {ca};\n"
             "    default_type application/x-x509-ca-cert;\n"
             "}\n") if ca is not None else ""
    if not other_devices or served is None or tls_port is None:
        return said + f"listen 127.0.0.1:{port} default_server;\nlisten [::1]:{port} default_server;\n" + trust
    return said + (
        f"listen {port} default_server;\nlisten [::]:{port} default_server;\n"
        f"listen {tls_port} ssl default_server;\nlisten [::]:{tls_port} ssl default_server;\n"
        f"ssl_certificate {served.certificate};\nssl_certificate_key {served.key};\n"
        "ssl_protocols TLSv1.2 TLSv1.3;\n"
        f"# serving {served.source}'s certificate, sha256 {served.fingerprint}\n"
        "# Plain HTTP is this machine's alone: another device is sent to HTTPS, but\n"
        "# for the authority's certificate, which it needs before it can trust HTTPS.\n"
        'set $to_https "";\n'
        "if ($scheme = http) { set $to_https A; }\n"
        'if ($remote_addr !~ "^(127\\.0\\.0\\.1|::1)$") { set $to_https "${to_https}B"; }\n'
        "if ($uri = /lumiverb-ca.crt) { set $to_https C; }\n"
        f"if ($to_https = AB) {{ return 301 https://$host:{tls_port}$request_uri; }}\n"
    ) + trust


def write_listen_file(data_dir: Path, port: int, other_devices: bool, *, tls_port: int | None = None, served=None,
                      ca: Path | None = None) -> bool:
    """The lines for the choice; True if they changed."""
    path = listen_file(data_dir)
    lines = listen_lines(port, other_devices, tls_port=tls_port, served=served, ca=ca)
    try:
        if path.read_text() == lines:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lines)
    return True


def apply(data_dir: Path, port: int, tls_port: int) -> bool:
    """The choice and the certificate written for nginx (tls.served: issued
    or renewed when it needs to be); True if they changed."""
    from src.server.lifecycle import tls

    on = other_devices(data_dir)
    served = tls.served(data_dir)
    return write_listen_file(data_dir, port, on, tls_port=tls_port, served=served, ca=tls.ca_certificate(data_dir))


def watch(data_dir: Path, port: int, tls_port: int, stop: threading.Event, restart: Callable[[], None],
          every: float = WATCH_EVERY_SEC) -> None:
    """Applies the choice and the certificate whenever either changes (a new
    address on the network, a renewal, an admin's), until `stop`: the lines
    rewritten, then nginx restarted (`restart`) to use them."""
    while not stop.wait(every):
        try:
            changed = apply(data_dir, port, tls_port)
        except Exception:  # noqa: BLE001 — said, and tried again on the next look
            logger.exception("Network: couldn't apply the choice or the certificate")
            continue
        if changed:
            logger.info("Network: other devices %s; restarting nginx",
                        "may use it, over HTTPS" if other_devices(data_dir) else "can't use it")
            restart()


def managed(data_dir: Path, nginx_config: str | None) -> bool:
    """This install's nginx takes its listen lines from the choice: run starts
    it (NGINX_CONFIG) and its config includes them."""
    if not nginx_config:
        return False
    try:
        return include_line(data_dir) in Path(nginx_config).read_text()
    except OSError:
        return False


def lan_address() -> str | None:
    """This machine's address on its network: the one it would reach out from
    (a UDP socket connects without sending anything)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed anywhere
            address = probe.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith("127.") else address


def addresses(port: int, scheme: str = "https") -> list[str]:
    """Where other devices reach it (over HTTPS; plain HTTP only for the
    authority's certificate): the Mac's name (Bonjour, .local), then its address."""
    name = socket.gethostname()
    if "." not in name:
        name += ".local"
    found = [f"{scheme}://{name}:{port}"]
    if (address := lan_address()) is not None:
        found.append(f"{scheme}://{address}:{port}")
    return found
