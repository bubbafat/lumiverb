"""Whether other devices on the network may use a Mac's Lumiverb: its own
nginx listens on this machine only (as a new install does: it's set up
there first), or on every address.

The choice is an admin's (Settings › Network: routers/system.py), kept in
DATA_DIR/network.json. `lumiverb-server run` owns nginx, so it applies it:
it writes the listen lines nginx's config includes (DATA_DIR/nginx/
listen.conf), at start and whenever the choice changes, and restarts nginx.
An nginx set up some other way (a Linux install's, by deploy-web.sh) isn't this
install's to change: it's "not available" here.
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


def listen_lines(port: int, other_devices: bool) -> str:
    said = "# lumiverb-server run's, from Settings › Network: rewritten when that changes.\n"
    if other_devices:
        return said + f"listen {port} default_server;\nlisten [::]:{port} default_server;\n"
    return said + f"listen 127.0.0.1:{port} default_server;\nlisten [::1]:{port} default_server;\n"


def write_listen_file(data_dir: Path, port: int, other_devices: bool) -> bool:
    """The listen lines for the choice; True if they changed."""
    path = listen_file(data_dir)
    lines = listen_lines(port, other_devices)
    try:
        if path.read_text() == lines:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lines)
    return True


def watch(data_dir: Path, port: int, stop: threading.Event, restart: Callable[[], None],
          every: float = WATCH_EVERY_SEC) -> None:
    """Applies the choice whenever it changes, until `stop`: the listen lines
    rewritten, then nginx restarted (`restart`) to listen on them."""
    while not stop.wait(every):
        on = other_devices(data_dir)
        if write_listen_file(data_dir, port, on):
            logger.info("Other devices on the network %s: restarting nginx", "may use it now" if on else "can't use it now")
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


def _lan_address() -> str | None:
    """This machine's address on its network: the one it would reach out from
    (a UDP socket connects without sending anything)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed anywhere
            address = probe.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith("127.") else address


def addresses(port: int) -> list[str]:
    """Where other devices reach it: the Mac's name (Bonjour, .local), then its address."""
    name = socket.gethostname()
    if "." not in name:
        name += ".local"
    found = [f"http://{name}:{port}"]
    if (address := _lan_address()) is not None:
        found.append(f"http://{address}:{port}")
    return found
