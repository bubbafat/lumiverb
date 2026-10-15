"""Which environment variables are secrets, so each process gets only those it needs.

The install's env file holds its keys and passwords. lumiverb-server run
gives each part only the secrets it needs (src/server/lifecycle/cli.py
server_children); the scheduler, once its settings are read, drops its own
(forget), so what it starts (ffmpeg, exiftool, its face and transcript
processes) has none; and the API's media tools get an environment without
them (without_secrets).

A secret: anything named like one (…PASSWORD…, …SECRET…, a whole word KEY
or TOKEN: ADMIN_KEY, API_SECRET_KEY, JWT_SECRET, SMTP_PASSWORD), or a value
holding a URL with a password in it (the databases': CONTROL_PLANE_DATABASE_URL,
TENANT_DATABASE_URL_TEMPLATE).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

_NAMED = re.compile(r"PASSWORD|SECRET|(^|_)(KEY|TOKEN)(_|$)", re.IGNORECASE)
_URL_WITH_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@:\s]*:[^@\s]+@", re.IGNORECASE)


def is_secret(name: str, value: str) -> bool:
    return bool(_NAMED.search(name) or _URL_WITH_PASSWORD.match(value or ""))


def without_secrets(env: Mapping[str, str], keep: Iterable[str] = ()) -> dict[str, str]:
    """`env` without its secrets, but for those named in `keep`."""
    kept = set(keep)
    return {name: value for name, value in env.items() if name in kept or not is_secret(name, value)}


def forget(keep: Iterable[str] = ()) -> list[str]:
    """Drops this process's secrets from its environment, but for `keep`, so
    nothing it starts from now on inherits them. Only once its settings are
    read (src/server/config.py get_settings caches them). Returns the names dropped."""
    kept = set(keep)
    dropped = [name for name, value in os.environ.items() if name not in kept and is_secret(name, value)]
    for name in dropped:
        del os.environ[name]
    return sorted(dropped)
