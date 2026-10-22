"""Which environment variables are secrets, so each process gets only those it needs.

The install's env file holds its keys and passwords. lumiverb-server run
gives each part only the secrets it needs (src/server/lifecycle/cli.py
server_children); on Linux, whose units hand each the whole env file, the
plug-in runner starts again with only its own (keep_only). The scheduler,
once its settings are read, drops its own (forget), so what it starts
(ffmpeg, exiftool, its face and transcript processes) has none, as does an
API worker served by python -m src.server.api (API_FORGETS); and the API's
tools get an environment without them (without_secrets).

A plug-in runs as the same user as the API, the scheduler and the runner,
and on Linux a process can read another of its user's environment
(/proc/<pid>/environ: the environment it started with, whatever was deleted
since) and, with ptrace, its memory. So each of them makes itself not
dumpable as it starts (not_dumpable), which closes both to every process
but root's. What runs as that user still shares its files (on a Mac, the
owner's whole account): confining a plug-in's is ADR-023's phase 4, deferred.

A secret: anything named like one (…PASSWORD…, …SECRET…, a whole word KEY
or TOKEN: ADMIN_KEY, API_SECRET_KEY, JWT_SECRET, SMTP_PASSWORD), or a value
holding a URL with a password in it (the databases': CONTROL_PLANE_DATABASE_URL,
TENANT_DATABASE_URL_TEMPLATE).
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping

logger = logging.getLogger(__name__)

_NAMED = re.compile(r"PASSWORD|SECRET|(^|_)(KEY|TOKEN)(_|$)", re.IGNORECASE)
_URL_WITH_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@:\s]*:[^@\s]+@", re.IGNORECASE)

# The scheduler's and the plug-in runner's: the databases' addresses (the scheduler reads them directly and makes its
# own API keys, scheduler/service.py scheduler_key; the server's settings require the template, config.py, though each
# account's database is read from its routing, with the same credentials as the control URL's), and the key that opens
# the secrets Lumiverb keeps sealed (src/server/sealing.py), plug-ins' secret settings among them, which both hand on.
SCHEDULER_SECRETS = ("CONTROL_PLANE_DATABASE_URL", "TENANT_DATABASE_URL_TEMPLATE", "LUMIVERB_SECRETS_KEY")

# prctl(2)'s options for a process's dumpable flag (linux/prctl.h).
PR_GET_DUMPABLE, PR_SET_DUMPABLE = 3, 4

# Set by python -m src.server.api (a Linux install's unit) for the workers it starts: each forgets the env file's
# secrets once its settings are read, as the scheduler does, so what it starts (its face process among them) has none.
# Never set elsewhere: tests import the app and read their settings again.
API_FORGETS = "LUMIVERB_API_FORGETS_SECRETS"


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


def started_without_secrets[T](start: Callable[[], T]) -> T:
    """`start()` with this process's secrets out of its environment for the while, so the program it starts has
    none; they're back after. Only while nothing else in the process runs (another thread would miss them)."""
    saved = {name: value for name, value in os.environ.items() if is_secret(name, value)}
    for name in saved:
        del os.environ[name]
    try:
        return start()
    finally:
        os.environ.update(saved)


def keep_only(keep: Iterable[str]) -> None:
    """This process started again, the same program with the same arguments (exec: the same process id), with no
    secret in its environment but those in `keep`, when it has others. The environment a process started with stays
    in its memory whatever is deleted from os.environ; a Linux unit hands it the whole env file. Called first, before
    anything is read: it doesn't return when it starts again, and the second time there's nothing to drop."""
    env = without_secrets(os.environ, keep=keep)
    if len(env) == len(os.environ):
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], env)


def not_dumpable() -> bool:
    """This process's memory and environment closed to every other process of its user, a plug-in's among them: on
    Linux, its dumpable flag off (prctl), so only root can read its /proc/<pid>/environ, its memory or attach to it
    (and it leaves no core file). Elsewhere nothing changes: on a Mac a plug-in runs as the Mac's owner, whose files
    (the env file among them) it can read anyway. Each program calls it as it starts: exec sets the flag again, a fork
    keeps it. Whether it's off now."""
    if not sys.platform.startswith("linux"):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        logger.warning("This process stays readable to the other processes of its user: prctl failed (%s)",
                       os.strerror(ctypes.get_errno()))
        return False
    return libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) == 0
