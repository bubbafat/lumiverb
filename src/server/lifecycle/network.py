"""Whether other devices on the network may use a Mac's Lumiverb: its own
nginx listens on this machine only (as a new install does: it's set up
there first), or for other devices too.

This machine's own browser keeps plain HTTP on localhost. Other devices get
plain HTTP there too: nothing to set up on them. HTTPS is an admin's to add,
on a port of its own, with the certificate tls.py serves (Lumiverb's own
authority's, which each device trusts once, at /lumiverb-ca.crt; or one an
admin gave). Plain HTTP stays on beside it until the admin turns it off,
which they can once a device has used HTTPS (`saw_https`): then a device
coming over plain HTTP is sent to HTTPS, but for the authority's certificate.

Each port carries the web and the API together (nginx passes /v1/ on):
6464 for plain HTTP and 6465 for HTTPS, the env file's WEB_PORT and
WEB_TLS_PORT where it names others, and whatever an admin chooses instead
(`ports`): never another of the install's ports nor one something else
listens on (`port_problem`), and back to the one before if nginx doesn't
start listening there after all (`watch`).

The choice is an admin's (Settings › Network: routers/system.py), kept in
DATA_DIR/network.json. `lumiverb-server run` owns nginx, so it applies it:
it writes the lines nginx's config includes in its server (DATA_DIR/nginx/
listen.conf), at start and whenever the choice or the certificate changes,
and restarts nginx. An nginx set up some other way (a Linux install's, by
deploy-web.sh) isn't this install's to change: it's "not available" here.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("lumiverb-server")

NETWORK_FILE = "network.json"
HTTPS_SEEN_FILE = "https-seen.json"
LISTEN_FILE = Path("nginx") / "listen.conf"
WATCH_EVERY_SEC = 2.0


def listen_file(data_dir: Path) -> Path:
    return data_dir / LISTEN_FILE


def include_line(data_dir: Path) -> str:
    """What nginx's config says to use the listen lines (init.py writes it)."""
    return f"include {listen_file(data_dir)};"


@dataclass(frozen=True)
class Choice:
    """An admin's: this machine only until they make one."""
    other_devices: bool = False
    https: bool = False  # also over HTTPS (tls.py's certificate)
    plain_http: bool = True  # other devices over plain HTTP too: off only beside HTTPS

    @property
    def http_for_others(self) -> bool:
        return self.other_devices and (self.plain_http or not self.https)

    @property
    def https_for_others(self) -> bool:
        return self.other_devices and self.https

    def said(self) -> str:
        if not self.other_devices:
            return "this machine only"
        if not self.https_for_others:
            return "other devices over HTTP"
        return "other devices over HTTP and HTTPS" if self.http_for_others else "other devices over HTTPS alone"


def _said(data_dir: Path) -> dict:
    try:
        said = json.loads((data_dir / NETWORK_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return said if isinstance(said, dict) else {}


def _say(data_dir: Path, **changed: object) -> None:
    """network.json with `changed` in it, the rest as it was: written whole."""
    path = data_dir / NETWORK_FILE
    temporary = path.with_name(f".{NETWORK_FILE}.new")
    temporary.write_text(json.dumps({**_said(data_dir), **changed}) + "\n")
    os.replace(temporary, path)


def choice(data_dir: Path) -> Choice:
    """The admin's choice; what can't be read, the default's."""
    said, default = _said(data_dir), Choice()
    return Choice(**{field.name: bool(said.get(field.name, getattr(default, field.name)))
                     for field in dataclasses.fields(Choice)})


def set_choice(data_dir: Path, chosen: Choice) -> None:
    _say(data_dir, **dataclasses.asdict(chosen))


# -- Ports ---------------------------------------------------------------------------------------

# Web and API together on each (nginx passes /v1/ on): 6464 plain HTTP, 6465
# HTTPS. The API itself listens on this machine alone, on a Mac's API_PORT
# (6364, init.py), which nothing else uses.
DEFAULT_HTTP_PORT = 6464
DEFAULT_TLS_PORT = 6465


@dataclass(frozen=True)
class Ports:
    http: int = DEFAULT_HTTP_PORT
    https: int = DEFAULT_TLS_PORT


def _port(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def default_ports(web_port: object = None, tls_port: object = None) -> Ports:
    """The install's: the env file's WEB_PORT and WEB_TLS_PORT, else 6464 and 6465."""
    return Ports(_port(web_port) or DEFAULT_HTTP_PORT, _port(tls_port) or DEFAULT_TLS_PORT)


def ports(data_dir: Path, defaults: Ports) -> Ports:
    """Where nginx listens: Settings › Network's ports, else the install's."""
    said = _said(data_dir)
    return Ports(_port(said.get("http_port")) or defaults.http, _port(said.get("https_port")) or defaults.https)


def set_ports(data_dir: Path, chosen: Ports) -> None:
    _say(data_dir, http_port=chosen.http, https_port=chosen.https)


def port_taken(env: dict[str, str], port: int) -> str | None:
    """Which of the install's own other ports `port` is, or None: nginx and
    that one can't both listen there (a Mac lets both bind, and then this
    machine's own connections reach the other)."""
    from sqlalchemy import make_url

    others = {"the API's port (API_PORT)": env.get("API_PORT")}
    if url := env.get("CONTROL_PLANE_DATABASE_URL"):
        try:
            others["the database's port"] = str(make_url(url).port or "")
        except Exception:  # noqa: BLE001 — not a URL: the database says why, elsewhere
            pass
    return next((name for name, value in others.items() if value and value.isdigit() and int(value) == port), None)


def free(port: int) -> bool:
    """Nothing on this machine listens on `port`, so nginx can: on every
    address, and on this machine's own (another program may hold only that,
    which a Mac would let nginx share: this machine's own connections would
    reach it). Bound as nginx binds (SO_REUSEADDR), so connections closed a
    moment ago (TIME_WAIT) don't count."""
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET, "0.0.0.0"),
                            (socket.AF_INET6, "::1"), (socket.AF_INET6, "::")):
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue  # no IPv6 here
        with probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((address, port))
            except OSError as exc:
                if exc.errno == errno.EADDRNOTAVAIL:
                    continue  # no such address here
                return False
    return True


def listened(chosen: Choice, at: Ports) -> frozenset[int]:
    """The ports nginx listens on for the choice: HTTP's always, HTTPS's
    while other devices are served it."""
    return frozenset({at.http, *((at.https,) if chosen.https_for_others else ())})


def port_problem(env: dict[str, str], chosen: Ports, *, held: frozenset[int] = frozenset()) -> str | None:
    """Why nginx can't listen on `chosen`, or None: the same port twice,
    another of the install's, or one something else listens on (`held`,
    nginx's own now, aren't asked about)."""
    if chosen.http == chosen.https:
        return f"HTTP and HTTPS can't share port {chosen.http}"
    for name, port in (("HTTP", chosen.http), ("HTTPS", chosen.https)):
        if taken := port_taken(env, port):
            return f"{name}'s port {port} is {taken}"
        if port not in held and not free(port):
            return f"Something else on this Mac is using port {port}: choose another for {name}"
    return None


@dataclass(frozen=True)
class Seen:
    at: datetime
    address: str  # the device's, as nginx saw it


def saw_https(data_dir: Path, address: str, now: datetime | None = None) -> bool:
    """A device reached it over HTTPS (the API sees every request: middleware.py):
    kept the first time since HTTPS last changed (`forget_https_seen`), and
    never written over, so only the first look costs more than a stat. True
    if this was the first."""
    path = data_dir / HTTPS_SEEN_FILE
    if path.exists():
        return False
    temporary = path.with_name(f".{HTTPS_SEEN_FILE}.{os.getpid()}.{threading.get_ident()}")
    temporary.write_text(json.dumps({"at": (now or datetime.now(UTC)).isoformat(), "address": address}) + "\n")
    try:
        os.link(temporary, path)  # whole, and only if there's none: another worker's may have just been
        return True
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)


def https_seen(data_dir: Path) -> Seen | None:
    """The first device seen using HTTPS since it last changed, or None."""
    try:
        said = json.loads((data_dir / HTTPS_SEEN_FILE).read_text())
        return Seen(at=datetime.fromisoformat(said["at"]), address=str(said["address"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def forget_https_seen(data_dir: Path) -> None:
    """HTTPS turned on or off, or another certificate: a device must use it again."""
    (data_dir / HTTPS_SEEN_FILE).unlink(missing_ok=True)


def listen_lines(port: int, chosen: Choice, *, tls_port: int | None = None, served=None,
                 ca: Path | None = None) -> str:
    """nginx's lines for the choice, in its server block: where it listens,
    the certificate it serves other devices over HTTPS (`served`, tls.Served:
    none, none), and the authority's certificate for them to trust (`ca`)."""
    said = "# lumiverb-server run's, from Settings › Network: rewritten when that changes.\n"
    if not chosen.other_devices:
        return said + f"listen 127.0.0.1:{port} default_server;\nlisten [::1]:{port} default_server;\n"
    lines = said + f"listen {port} default_server;\nlisten [::]:{port} default_server;\n"
    if not chosen.https_for_others or served is None or tls_port is None:
        return lines
    lines += (
        f"listen {tls_port} ssl default_server;\nlisten [::]:{tls_port} ssl default_server;\n"
        f"ssl_certificate {served.certificate};\nssl_certificate_key {served.key};\n"
        "ssl_protocols TLSv1.2 TLSv1.3;\n"
        "http2 on;  # HTTPS's: a browser keeps HTTP/1.1 on plain HTTP\n"
        f"# serving {served.source}'s certificate, sha256 {served.fingerprint}\n"
    )
    if not chosen.http_for_others:
        lines += (
            "# Plain HTTP is this machine's alone: another device is sent to HTTPS, but\n"
            "# for the authority's certificate, which it needs before it can trust HTTPS.\n"
            'set $to_https "";\n'
            "if ($scheme = http) { set $to_https A; }\n"
            'if ($remote_addr !~ "^(127\\.0\\.0\\.1|::1)$") { set $to_https "${to_https}B"; }\n'
            "if ($uri = /lumiverb-ca.crt) { set $to_https C; }\n"
            f"if ($to_https = AB) {{ return 301 https://$host:{tls_port}$request_uri; }}\n"
        )
    if ca is not None:
        lines += ("location = /lumiverb-ca.crt {\n"
                  f"    alias {ca};\n"
                  "    default_type application/x-x509-ca-cert;\n"
                  "}\n")
    return lines


def write_listen_file(data_dir: Path, port: int, chosen: Choice, *, tls_port: int | None = None, served=None,
                      ca: Path | None = None) -> bool:
    """The lines for the choice; True if they changed."""
    path = listen_file(data_dir)
    lines = listen_lines(port, chosen, tls_port=tls_port, served=served, ca=ca)
    try:
        if path.read_text() == lines:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lines)
    return True


def apply(data_dir: Path, defaults: Ports) -> bool:
    """The choice written for nginx, on its ports (`ports`), with the
    certificate when it's HTTPS too (tls.served: made, issued again or renewed
    when it needs to be; none made until then); True if the lines changed."""
    from src.server.lifecycle import tls

    chosen, at = choice(data_dir), ports(data_dir, defaults)
    served = tls.served(data_dir) if chosen.https_for_others else None
    ca = tls.ca_certificate(data_dir) if served is not None and served.source == tls.LUMIVERB else None
    return write_listen_file(data_dir, at.http, chosen, tls_port=at.https, served=served, ca=ca)


def without_https(data_dir: Path, defaults: Ports) -> None:
    """The choice but HTTPS, for when its certificate can't be made: other
    devices keep plain HTTP (the watch tries HTTPS again)."""
    write_listen_file(data_dir, ports(data_dir, defaults).http, dataclasses.replace(choice(data_dir), https=False))


PORT_WAIT_SEC = 30.0


def listening(port: int, stop: threading.Event, within: float = PORT_WAIT_SEC) -> bool:
    """Something answers on this machine's `port` within `within` seconds."""
    deadline = time.monotonic() + within
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            if time.monotonic() >= deadline or stop.wait(0.25):
                return False


def said_with_ports(chosen: Choice, at: Ports) -> str:
    https = f" and {at.https} (HTTPS)" if chosen.https_for_others else ""
    return f"{chosen.said()}, on port {at.http}{https}"


def watch(data_dir: Path, defaults: Ports, stop: threading.Event, restart: Callable[[], None],
          every: float = WATCH_EVERY_SEC, *, answers: Callable[[int, threading.Event], bool] = listening) -> None:
    """Applies the choice and the certificate whenever either changes (a new
    address on the network, a renewal, an admin's), until `stop`: the lines
    rewritten, then nginx restarted (`restart`) to use them. A port it
    should now listen on and doesn't (`answers`: something took it since the
    admin chose it, and nginx won't start) puts the choice and the ports back
    as they were, so this machine can still open Settings."""
    was = (choice(data_dir), ports(data_dir, defaults))
    while not stop.wait(every):
        try:
            changed = apply(data_dir, defaults)
        except Exception:  # noqa: BLE001 — said, and tried again on the next look
            logger.exception("Network: couldn't apply the choice or the certificate")
            continue
        if not changed:
            continue
        chosen, at = choice(data_dir), ports(data_dir, defaults)
        logger.info("Network: %s; restarting nginx", said_with_ports(chosen, at))
        restart()
        new = sorted(listened(chosen, at) - listened(*was))
        failed = next((port for port in new if not answers(port, stop)), None)
        if failed is None:
            was = (chosen, at)
        elif not stop.is_set():
            logger.error("Network: nginx isn't listening on port %d: back to %s", failed, said_with_ports(*was))
            set_choice(data_dir, was[0])
            set_ports(data_dir, was[1])


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


def addresses(port: int, scheme: str) -> list[str]:
    """Where other devices reach it on `port` ("http" or "https"): the Mac's
    name (Bonjour, .local), then its address."""
    name = socket.gethostname()
    if "." not in name:
        name += ".local"
    found = [f"{scheme}://{name}:{port}"]
    if (address := lan_address()) is not None:
        found.append(f"{scheme}://{address}:{port}")
    return found
