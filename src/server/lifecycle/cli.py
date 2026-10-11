"""lumiverb-server: look after this machine's Lumiverb server.

    lumiverb-server migrate --env FILE
    lumiverb-server run --env FILE

migrate: the control plane's and every account's database migrations, as
scripts/migrate.sh does on the brain.

run: the server as one long-running service, for a Mac (Homebrew's
service; launchd keeps it up). Migrates first, waiting for Postgres if it
isn't up yet, then runs the API and the scheduler as the brain's systemd
units do (supervise.py) and posts upkeep as its timers do (upkeep.py), until
SIGTERM. The brain keeps its systemd units: it doesn't use this.

Which install is always said, never assumed: --env FILE, or
$LUMIVERB_ENV_FILE, or CONTROL_PLANE_DATABASE_URL already in the
environment (a dev checkout). On the brain /etc/lumiverb/env is prod's, so a
dev checkout run without one would migrate prod: it's refused instead. A
variable already in the environment wins over the file's, as for the
server's own settings.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from src.server.lifecycle import upkeep
from src.server.lifecycle.envfile import EnvFileError, read_env_file
from src.server.lifecycle.migrate import APP_DIR, MigrationError, migrate
from src.server.lifecycle.supervise import Child, Supervisor

logger = logging.getLogger("lumiverb-server")

NO_INSTALL = ("Say which install: --env FILE or $LUMIVERB_ENV_FILE, or CONTROL_PLANE_DATABASE_URL in the "
              "environment. None is assumed (on the brain, /etc/lumiverb/env is prod's).")
DEFAULT_API_PORT = "8000"  # update.sh's default when the env file has none
DEFAULT_API_WORKERS = "2"  # the brain's unit: uvicorn --workers 2
MIGRATE_RETRY_MAX_SEC = 60.0


class InstallError(Exception):
    pass


def install_env(env_file: Path | None) -> dict[str, str]:
    """The named env file's variables under the environment's own. Raises
    InstallError when no install is named or the file can't be read."""
    path = env_file or (Path(os.environ["LUMIVERB_ENV_FILE"]) if os.environ.get("LUMIVERB_ENV_FILE") else None)
    if path is None:
        if not os.environ.get("CONTROL_PLANE_DATABASE_URL"):
            raise InstallError(NO_INSTALL)
        return dict(os.environ)
    try:
        from_file = read_env_file(path)
    except FileNotFoundError:
        raise InstallError(f"No env file at {path}") from None
    except OSError as exc:
        raise InstallError(f"Can't read {path}: {exc.strerror or exc}") from None
    return {**from_file, **os.environ}


def data_dir(env: dict[str, str]) -> Path:
    """DATA_DIR, else the install's data/. Raises InstallError when it isn't a
    full path: run empties folders under it, so it's never taken from
    wherever the service started."""
    path = Path(env.get("DATA_DIR") or APP_DIR / "data")
    if not path.is_absolute():
        raise InstallError(f"DATA_DIR must be a full path, not {path}")
    return path


def server_children(env: dict[str, str], *, python: str = sys.executable, cwd: Path = APP_DIR) -> list[Child]:
    """The API and the scheduler, with the brain's units' environment, restart
    delays and stop times (deploy-api.sh), API first."""
    base = {**env, "PYTHONUNBUFFERED": "1"}
    data = data_dir(env)
    api = Child(
        "api",
        [python, "-m", "uvicorn", "src.server.api.main:app",
         "--host", env.get("API_LISTEN_HOST") or "127.0.0.1",
         "--port", env.get("API_PORT") or DEFAULT_API_PORT,
         "--workers", env.get("LUMIVERB_API_WORKERS") or DEFAULT_API_WORKERS],
        # Large uploads (analysis proxies) are spooled on the data disk.
        {**base, "TMPDIR": str(data / "tmp")}, cwd,
        restart_after=5, stop_within=90, empty_first=data / "tmp",
    )
    scheduler = Child(
        "scheduler",
        [python, "-m", "src.server.scheduler"],
        {**base, "XDG_CACHE_HOME": str(data / "cache"), "TMPDIR": str(data / "worker-tmp")}, cwd,
        # It lets jobs in hand finish for up to 25 s; only it hears the stop.
        restart_after=30, stop_within=40, term_main_only=True, empty_first=data / "worker-tmp",
    )
    return [api, scheduler]


def migrate_when_ready(url: str, stop: threading.Event, *, migrate_fn: Callable[[str], int] = migrate,
                       first_wait: float = 5.0) -> bool:
    """Migrates, trying again (5 s, doubling to a minute) while it can't:
    Postgres may still be starting at login. False if stopped first."""
    wait = first_wait
    while not stop.is_set():
        try:
            migrate_fn(url)
            return True
        except Exception as exc:  # noqa: BLE001 — Postgres not up yet, or a migration failing: said, then tried again
            logger.warning("Migrations didn't run (%s); trying again in %.0f s", exc, wait)
            stop.wait(wait)
            wait = min(wait * 2, MIGRATE_RETRY_MAX_SEC)
    return False


def _env_and_url(args: argparse.Namespace) -> tuple[dict[str, str], str]:
    env = install_env(args.env)
    url = env.get("CONTROL_PLANE_DATABASE_URL")
    if not url:
        raise InstallError(f"No CONTROL_PLANE_DATABASE_URL in {args.env or os.environ.get('LUMIVERB_ENV_FILE')}")
    return env, url


def _migrate(args: argparse.Namespace) -> int:
    _, url = _env_and_url(args)
    try:
        count = migrate(url)
    except MigrationError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ Migrated the control plane and {count} account(s)")
    return 0


def _run(args: argparse.Namespace) -> int:
    env, url = _env_and_url(args)
    data_dir(env)  # before anything starts
    logging.basicConfig(level=logging.INFO, format="lumiverb-server: %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())

    if not migrate_when_ready(url, stop):
        return 0
    base_url = f"http://127.0.0.1:{env.get('API_PORT') or DEFAULT_API_PORT}"
    if env.get("ADMIN_KEY"):
        schedule = upkeep.Schedule(datetime.now(), data_dir(env) / "upkeep-state.json")
        threading.Thread(
            target=upkeep.run_upkeep, name="upkeep", daemon=True,
            args=(schedule, stop),
            kwargs={"is_healthy": lambda: upkeep.healthy(base_url),
                    "send": lambda path: upkeep.post(base_url, path, env["ADMIN_KEY"])},
        ).start()
    else:
        logger.warning("No ADMIN_KEY: upkeep (search sync, trash purge, cleanup) won't run")
    Supervisor(server_children(env)).run(stop)
    return 0


def _env_option(parser: argparse.ArgumentParser, default: object) -> None:
    parser.add_argument("--env", type=Path, default=default,
                        help="the install's env file (or $LUMIVERB_ENV_FILE); none is assumed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumiverb-server", description="Look after this machine's Lumiverb server.")
    _env_option(parser, None)
    commands = parser.add_subparsers(dest="command", required=True)
    # --env before or after the command. A subcommand's default would overwrite
    # one given before it, so it sets --env only when given there.
    _env_option(commands.add_parser("migrate", help="migrate the control plane and every account's database"),
                argparse.SUPPRESS)
    _env_option(commands.add_parser(
        "run", help="migrate, then run the API, the scheduler and upkeep until stopped (a Mac's service)"),
        argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return {"migrate": _migrate, "run": _run}[args.command](args)
    except (InstallError, EnvFileError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


def entry() -> None:
    sys.exit(main())
