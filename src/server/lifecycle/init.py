"""The files an install needs, each written once: its env file, Quickwit's
config and nginx's site. Homebrew's post_install runs it on every install
and upgrade, so a file already there is kept as it is: init never rewrites
one (move it away and run init again to start over).

The env file is deploy-api.sh's, for a Mac: Homebrew's PostgreSQL as you,
secrets generated here. Quickwit's config and nginx's site are made from
the env file, whichever wrote it, so they agree with it. The nginx site is
deploy-web.sh's (a test holds them to the same bytes).
"""

from __future__ import annotations

import getpass
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.server.lifecycle.envfile import read_env_file
from src.server.lifecycle.migrate import APP_DIR

# A block of their own, clear of a dev checkout's (Vite 5173, uvicorn 8000,
# docker-compose's Quickwit on 7280 and 7281).
WEB_PORT = 6464
API_PORT = 6465
QUICKWIT_PORT = 6466  # gRPC on the next one, as deploy-api.sh does
PG_PORT = 5432  # Homebrew's PostgreSQL
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

    def given(self) -> dict[str, str]:
        named = {"DATA_DIR": self.data_dir, "PG_PORT": self.pg_port, "API_PORT": self.api_port,
                 "QUICKWIT_PORT": self.quickwit_port, "WEB_PORT": self.web_port, "APP_HOST": self.app_host}
        return {key: str(value) for key, value in named.items() if value is not None}


def env_text(choices: Choices, *, user: str, now: datetime, secret: Callable[[], str]) -> str:
    if choices.data_dir is None:
        raise InitError("Say where its data goes: --data-dir DIR")
    data = choices.data_dir
    pg_port = choices.pg_port or PG_PORT
    api_port = choices.api_port or API_PORT
    qw_port = choices.quickwit_port or QUICKWIT_PORT
    web_port = choices.web_port or WEB_PORT
    db = f"postgresql+psycopg2://{user}@127.0.0.1:{pg_port}"
    return f"""\
# Written by lumiverb-server init, {now.strftime('%Y-%m-%dT%H:%M:%SZ')}. Yours to edit:
# init never rewrites it (move it away and run init again to start over).

# Database (Homebrew's PostgreSQL, as you)
CONTROL_PLANE_DATABASE_URL={db}/control_plane
TENANT_DATABASE_URL_TEMPLATE={db}/{{tenant_id}}

# Auth
ADMIN_KEY={secret()}
API_SECRET_KEY={secret()}
JWT_SECRET={secret()}

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
PG_PORT={pg_port}
QUICKWIT_PORT={qw_port}
WEB_PORT={web_port}

# App
APP_ENV=production
APP_HOST={choices.app_host or f'http://localhost:{web_port}'}
LOG_LEVEL=INFO

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


def this_machine_only(port: int) -> tuple[str, ...]:
    """nginx on this machine's own addresses only: a new install is set up
    there (routers/setup.py) before anyone opens it to other devices."""
    return (f"listen 127.0.0.1:{port} default_server;", f"listen [::1]:{port} default_server;")


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


def init(env_file: Path, choices: Choices, *, quickwit_config: Path | None = None, nginx_site_file: Path | None = None,
         web_root: Path = WEB_ROOT, say: Callable[[str], None] = print,
         user: str | None = None, secret: Callable[[], str] = lambda: secrets.token_hex(32)) -> None:
    """Writes the env file if there's none, then Quickwit's config and the
    nginx site (each if named and not there) from it, and makes its data
    folders. Raises InitError when a given choice disagrees with the env file
    already there."""
    if env_file.exists():
        env = read_env_file(env_file)
        differ = [f"{key}={env.get(key, '')!r} (you said {value!r})"
                  for key, value in choices.given().items() if env.get(key) != value]
        if differ:
            raise InitError(f"{env_file} is already there and says " + ", ".join(differ)
                            + ". init doesn't change it: edit it, or move it away to start again.")
        say(f"· {env_file} kept")
    else:
        text = env_text(choices, user=user or getpass.getuser(), now=datetime.now(UTC), secret=secret)
        write_new(env_file, text, 0o600)
        env = read_env_file(env_file)
        say(f"✓ {env_file}")

    data = Path(env.get("DATA_DIR") or APP_DIR / "data")
    if not data.is_absolute():
        raise InitError(f"DATA_DIR must be a full path, not {data}")
    for folder in (data, data / "quickwit", data / "cache", data / "tmp", data / "worker-tmp"):
        folder.mkdir(parents=True, exist_ok=True)

    if quickwit_config is not None:
        text = quickwit_yaml(data, _port(env, "QUICKWIT_PORT", env_file))
        say(f"✓ {quickwit_config}" if write_new(quickwit_config, text, 0o644) else f"· {quickwit_config} kept")
    if nginx_site_file is not None:
        text = nginx_site(listen=this_machine_only(_port(env, "WEB_PORT", env_file)), server_name="_",
                          root=web_root, upstream=f"http://127.0.0.1:{_port(env, 'API_PORT', env_file)}")
        say(f"✓ {nginx_site_file}" if write_new(nginx_site_file, text, 0o644) else f"· {nginx_site_file} kept")
