"""The files an install needs, each written once: its env file, Quickwit's
config and nginx's. Homebrew's post_install runs it on every install and
upgrade, so a file already there is kept as it is: init never rewrites one
(move it away and run init again to start over).

The env file is deploy-api.sh's, for a Mac, where `lumiverb-server run` is
the one service: it names what run starts besides the API and the scheduler
(Lumiverb's own PostgreSQL, Quickwit and nginx: each one named), with
secrets generated here. Quickwit's config and nginx's are made from the env
file, whichever wrote it, so they agree with it. nginx's server block is
deploy-web.sh's (a test holds them to the same bytes).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.server.lifecycle import network
from src.server.lifecycle.envfile import read_env_file
from src.server.lifecycle.migrate import APP_DIR

# A block of their own, clear of a dev checkout's (Vite 5173, uvicorn 8000,
# docker-compose's Quickwit on 7280 and 7281).
WEB_PORT = 6464
API_PORT = 6465
QUICKWIT_PORT = 6466  # gRPC on the next one, as deploy-api.sh does
PG_PORT = 6468  # Lumiverb's own PostgreSQL (not Homebrew's, on 5432)
PG_ROLE = "lumiverb"
# The scheduler reads every library folder where it is: this machine has the files.
LOCAL_ROOT_MAP = '{"/": "/"}'
WEB_ROOT = APP_DIR / "src" / "ui" / "web" / "dist"

# deploy-web.sh's: the built app loads only its own files and calls only its
# own /v1/: no inline script or style, images shown from blob: URLs, video
# from /v1/stream/.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self'; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
       "frame-ancestors 'none'")


class InitError(Exception):
    pass


@dataclass(frozen=True)
class Choices:
    """What a new env file says. Given ones (not None) must agree with an
    env file that's already there."""
    data_dir: Path | None = None
    pg_port: int | None = None
    api_port: int | None = None
    quickwit_port: int | None = None
    web_port: int | None = None
    app_host: str | None = None
    # What run starts besides the API and the scheduler, each only if named.
    pg_bin: Path | None = None  # PostgreSQL's bin folder (initdb, postgres)
    quickwit_bin: Path | None = None
    quickwit_config: Path | None = None
    nginx_bin: Path | None = None
    nginx_config: Path | None = None
    local_roots: bool = False  # LUMIVERB_ROOT_MAP: library folders read where they are
    # LUMIVERB_RESET_ON_THIS_MACHINE: the person at this machine owns the install
    reset_on_this_machine: bool = False

    def runs(self) -> dict[str, str]:
        named = {"PG_BIN": self.pg_bin, "QUICKWIT_BIN": self.quickwit_bin, "QUICKWIT_CONFIG": self.quickwit_config,
                 "NGINX_BIN": self.nginx_bin, "NGINX_CONFIG": self.nginx_config}
        return {key: str(value) for key, value in named.items() if value is not None}

    def given(self) -> dict[str, str]:
        named = {"DATA_DIR": self.data_dir, "API_PORT": self.api_port, "QUICKWIT_PORT": self.quickwit_port,
                 "WEB_PORT": self.web_port, "APP_HOST": self.app_host}
        roots = {"LUMIVERB_ROOT_MAP": LOCAL_ROOT_MAP} if self.local_roots else {}
        reset = {"LUMIVERB_RESET_ON_THIS_MACHINE": "true"} if self.reset_on_this_machine else {}
        return {key: str(value) for key, value in named.items() if value is not None} | self.runs() | roots | reset


def env_text(choices: Choices, *, now: datetime, secret: Callable[[], str]) -> str:
    if choices.data_dir is None:
        raise InitError("Say where its data goes: --data-dir DIR")
    data = choices.data_dir
    api_port = choices.api_port or API_PORT
    qw_port = choices.quickwit_port or QUICKWIT_PORT
    web_port = choices.web_port or WEB_PORT
    admin_key, api_secret_key, jwt_secret, pg_password = secret(), secret(), secret(), secret()
    db = f"postgresql+psycopg2://{PG_ROLE}:{pg_password}@127.0.0.1:{choices.pg_port or PG_PORT}"
    runs = "".join(f"{key}={value}\n" for key, value in choices.runs().items())
    roots = (f"""
# Library folders: the scheduler reads each one where it is (this machine has
# the files). A Linux install maps another machine's paths instead.
LUMIVERB_ROOT_MAP='{LOCAL_ROOT_MAP}'
""" if choices.local_roots else "")
    reset = ("""
# Forgot password, on this machine itself: the reset link at once (whoever is
# at it owns this install). Never on a server: email, or lumiverb-server
# reset-password.
LUMIVERB_RESET_ON_THIS_MACHINE=true
""" if choices.reset_on_this_machine else "")
    return f"""\
# Written by lumiverb-server init, {now.strftime('%Y-%m-%dT%H:%M:%SZ')}. Yours to edit:
# init never rewrites it (move it away and run init again to start over).

# Database: Lumiverb's own PostgreSQL (its data in DATA_DIR/postgres, on
# this machine only), as its role, with this password
CONTROL_PLANE_DATABASE_URL={db}/control_plane
TENANT_DATABASE_URL_TEMPLATE={db}/{{tenant_id}}

# Auth
ADMIN_KEY={admin_key}
API_SECRET_KEY={api_secret_key}
JWT_SECRET={jwt_secret}

# Storage
STORAGE_PROVIDER=local
DATA_DIR={data}
XDG_CACHE_HOME={data / 'cache'}

# Search
QUICKWIT_URL=http://127.0.0.1:{qw_port}
QUICKWIT_ENABLED=true

# API (the web reaches it through nginx on WEB_PORT)
API_LISTEN_HOST=127.0.0.1
API_PORT={api_port}

# What this install uses
QUICKWIT_PORT={qw_port}
WEB_PORT={web_port}

# What lumiverb-server run starts besides the API and the scheduler
{runs}{roots}
# App
APP_ENV=production
APP_HOST={choices.app_host or f'http://localhost:{web_port}'}
LOG_LEVEL=INFO
{reset}
# Password reset SMTP (uncomment and fill in to enable forgot-password)
# SMTP_HOST=smtp.example.com
# SMTP_PORT=587
# SMTP_USER=apikey
# SMTP_PASSWORD=
# SMTP_FROM=noreply@lumiverb.local
"""


def quickwit_yaml(data_dir: Path, port: int) -> str:
    """deploy-api.sh's quickwit.yaml."""
    return f"""\
version: 0.8
node_id: lumiverb
listen_address: 127.0.0.1
rest:
  listen_port: {port}
grpc_listen_port: {port + 1}
data_dir: {data_dir / 'quickwit'}
"""


# Every response's headers: repeated in a location with add_header of its own,
# which drops the server's.
_HEADERS = (
    "add_header X-Content-Type-Options nosniff always;",
    f'add_header Content-Security-Policy "{CSP}" always;',
    "add_header X-Frame-Options DENY always;",
    "add_header Referrer-Policy no-referrer-when-downgrade always;",
)
_PROXIED = (
    "proxy_http_version 1.1;",
    "proxy_set_header Host $host;",
    "proxy_set_header X-Real-IP $remote_addr;",
    "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
    "proxy_set_header X-Forwarded-Proto $scheme;",
)


def _at(indent: int, lines: tuple[str, ...]) -> str:
    return ("\n" + " " * indent).join(lines)


def nginx_site(*, listen: tuple[str, ...], server_name: str, root: Path, upstream: str) -> str:
    """deploy-web.sh's site: the web build, with /v1/ (and playback, unbuffered)
    passed to the API."""
    return f"""\
server {{
    {_at(4, listen)}
    server_name {server_name};

    root {root};
    index index.html;

    {_at(4, _HEADERS)}

    # Playback: a multi-GB proxy streams straight through instead of being
    # spooled to disk, and the link (a bearer token good for hours) stays
    # out of the access log. Range requests pass through as they are.
    location /v1/stream/ {{
        proxy_pass {upstream};
        {_at(8, _PROXIED)}
        proxy_buffering off;
        proxy_request_buffering off;
        access_log off;
        proxy_connect_timeout 60s;
        proxy_read_timeout 300s;
    }}

    location /v1/ {{
        proxy_pass {upstream};
        {_at(8, _PROXIED)}
        client_max_body_size 100m;
        proxy_connect_timeout 60s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }}

    # Built files carry a content hash in their name: cache them for good.
    # A location's add_header drops the server's, so they are repeated.
    location /assets/ {{
        add_header Cache-Control "public, max-age=31536000, immutable";
        {_at(8, _HEADERS)}
    }}

    # The page names the build's files, so the browser checks it every time
    # (the SPA fallback below ends here too).
    location = /index.html {{
        add_header Cache-Control "no-cache";
        {_at(8, _HEADERS)}
    }}

    location / {{
        try_files $uri $uri/ /index.html;
    }}
}}
"""


# What the web build serves. nginx's own list isn't read: this config stands alone.
_TYPES = (
    "text/html html;", "text/css css;", "application/javascript js mjs;", "application/json json;",
    "application/manifest+json webmanifest;", "application/wasm wasm;", "text/plain txt;",
    "image/svg+xml svg;", "image/png png;", "image/jpeg jpg jpeg;", "image/webp webp;", "image/gif gif;",
    "image/x-icon ico;", "font/woff2 woff2;", "font/woff woff;",
)


def nginx_conf(site: str) -> str:
    """A whole nginx config around the site, for the install's own nginx: run
    starts it in the foreground with -p DATA_DIR/nginx (its pid and temp files
    there) and its errors in the service's log."""
    indented = "\n".join(f"    {line}" if line else "" for line in site.splitlines())
    return f"""\
# Lumiverb's own nginx: lumiverb-server run starts it (-p DATA_DIR/nginx).
daemon off;
error_log stderr;
pid nginx.pid;
worker_processes 1;

events {{
    worker_connections 256;
}}

http {{
    types {{
        {_at(8, _TYPES)}
    }}
    default_type application/octet-stream;
    sendfile on;
    access_log off;

{indented}
}}
"""


def write_new(path: Path, text: str, mode: int) -> bool:
    """Writes the file if there's none; False (and nothing written) if there is."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as out:
        out.write(text)
    return True


def _port(env: dict[str, str], key: str, env_file: Path) -> int:
    value = env.get(key, "")
    if not value.isdigit():
        raise InitError(f"{env_file} has no {key}" + (f" (it says {value!r})" if value else ""))
    return int(value)


def init(env_file: Path, choices: Choices, *, web_root: Path = WEB_ROOT, say: Callable[[str], None] = print,
         secret: Callable[[], str] = lambda: secrets.token_hex(32)) -> None:
    """Writes the env file if there's none, then Quickwit's config and
    nginx's (each where the env file names it, if it isn't there) from it,
    and makes its data folders. Raises InitError when a given choice
    disagrees with the env file already there."""
    if env_file.exists():
        env = read_env_file(env_file)
        differ = [f"{key}={env.get(key, '')!r} (you said {value!r})"
                  for key, value in choices.given().items() if env.get(key) != value]
        if differ:
            raise InitError(f"{env_file} is already there and says " + ", ".join(differ)
                            + ". init doesn't change it: edit it, or move it away to start again.")
        say(f"· {env_file} kept")
    else:
        text = env_text(choices, now=datetime.now(UTC), secret=secret)
        write_new(env_file, text, 0o600)
        env = read_env_file(env_file)
        say(f"✓ {env_file}")

    data = Path(env.get("DATA_DIR") or APP_DIR / "data")
    if not data.is_absolute():
        raise InitError(f"DATA_DIR must be a full path, not {data}")
    for folder in (data, data / "quickwit", data / "cache", data / "tmp", data / "worker-tmp", data / "nginx"):
        folder.mkdir(parents=True, exist_ok=True)

    if env.get("QUICKWIT_CONFIG"):
        path = Path(env["QUICKWIT_CONFIG"])
        text = quickwit_yaml(data, _port(env, "QUICKWIT_PORT", env_file))
        say(f"✓ {path}" if write_new(path, text, 0o644) else f"· {path} kept")
    if env.get("NGINX_CONFIG"):
        # Where it listens is Settings › Network's (network.py): this machine
        # only, as a new install is set up there (routers/setup.py), until an
        # admin says other devices may use it. run rewrites the lines then.
        path = Path(env["NGINX_CONFIG"])
        web_port = _port(env, "WEB_PORT", env_file)
        site = nginx_site(listen=(network.include_line(data),), server_name="_",
                          root=web_root, upstream=f"http://127.0.0.1:{_port(env, 'API_PORT', env_file)}")
        say(f"✓ {path}" if write_new(path, nginx_conf(site), 0o644) else f"· {path} kept")
        network.write_listen_file(data, web_port, network.other_devices(data))
