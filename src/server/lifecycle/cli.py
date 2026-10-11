"""lumiverb-server: look after this machine's Lumiverb server.

    lumiverb-server init --env FILE --data-dir DIR [--quickwit-config FILE] [--nginx-site FILE]
    lumiverb-server migrate --env FILE
    lumiverb-server run --env FILE
    lumiverb-server setup --env FILE [--email EMAIL] [--account-name NAME]

init: the install's env file, Quickwit's config and the nginx site, each
written only if it isn't there (init.py), for Homebrew's post_install.

migrate: the control plane's and every account's database migrations, as
scripts/migrate.sh does on the brain.

run: the server as one long-running service, for a Mac (Homebrew's
service; launchd keeps it up). Makes its control database if it's missing,
and migrates, waiting for Postgres if it isn't up yet; then runs the API and the scheduler as the brain's systemd
units do (supervise.py) and posts upkeep as its timers do (upkeep.py), until
SIGTERM. The brain keeps its systemd units: it doesn't use this.

setup: the install's first admin, asking for their email and password; the
same as the web app's setup page (src/server/setup.py), while nobody can
log in yet.

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
from getpass import getpass
from pathlib import Path

from src.server.lifecycle import upkeep
from src.server.lifecycle.envfile import EnvFileError, read_env_file
from src.server.lifecycle.init import WEB_ROOT, Choices, InitError, init
from src.server.lifecycle.migrate import (
    APP_DIR,
    MigrationError,
    create_database_if_missing,
    migrate,
)
from src.server.lifecycle.supervise import Child, Supervisor

logger = logging.getLogger("lumiverb-server")

NO_INSTALL = ("Say which install: --env FILE or $LUMIVERB_ENV_FILE, or CONTROL_PLANE_DATABASE_URL in the "
              "environment. None is assumed (on the brain, /etc/lumiverb/env is prod's).")
DEFAULT_API_PORT = "8000"  # update.sh's default when the env file has none
DEFAULT_API_WORKERS = "2"  # the brain's unit: uvicorn --workers 2
MIGRATE_RETRY_MAX_SEC = 60.0


class InstallError(Exception):
    pass


def _named_env_file(env_file: Path | None) -> Path | None:
    return env_file or (Path(os.environ["LUMIVERB_ENV_FILE"]) if os.environ.get("LUMIVERB_ENV_FILE") else None)


def install_env(env_file: Path | None) -> dict[str, str]:
    """The named env file's variables under the environment's own. Raises
    InstallError when no install is named or the file can't be read."""
    path = _named_env_file(env_file)
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


def _init(args: argparse.Namespace) -> int:
    env_file = _named_env_file(args.env)
    if env_file is None:
        raise InstallError("Say which env file: --env FILE or $LUMIVERB_ENV_FILE")
    choices = Choices(data_dir=args.data_dir.absolute() if args.data_dir else None,
                      pg_port=args.pg_port, api_port=args.api_port,
                      quickwit_port=args.quickwit_port, web_port=args.web_port, app_host=args.app_host)
    try:
        init(env_file, choices, quickwit_config=args.quickwit_config, nginx_site_file=args.nginx_site,
             web_root=args.web_root)
    except InitError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"✗ Can't write {exc.filename}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    return 0


def _migrate(args: argparse.Namespace) -> int:
    _, url = _env_and_url(args)
    try:
        count = migrate(url)
    except MigrationError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ Migrated the control plane and {count} account(s)")
    return 0


def _created_and_migrated(url: str) -> int:
    """Its control database made if missing, then migrated. Said through the
    log: print()'s lines wait in a buffer when the log is a file (launchd's)."""
    if create_database_if_missing(url):
        logger.info("Created the control database")
    return migrate(url, say=logger.info)


def _run(args: argparse.Namespace) -> int:
    env, url = _env_and_url(args)
    data_dir(env)  # before anything starts
    logging.basicConfig(level=logging.INFO, format="lumiverb-server: %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())

    if not migrate_when_ready(url, stop, migrate_fn=_created_and_migrated):
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


def _setup(args: argparse.Namespace) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from src.server.accounts import AccountError
    from src.server.config import get_settings
    from src.server.database import get_control_session
    from src.server.setup import ALREADY_SET_UP, SetupError, create_first_admin, setup_needed

    env, _ = _env_and_url(args)
    os.environ.update(env)  # the server's settings are the environment's (src/server/config.py)
    get_settings.cache_clear()

    try:
        with get_control_session() as session:
            needed = setup_needed(session)  # said before asking for anything
        if not needed:
            print(f"✗ {ALREADY_SET_UP}", file=sys.stderr)
            return 1
        email = args.email or input("Email: ")
        password = getpass("Password: ")
        if getpass("Password again: ") != password:
            print("✗ The passwords don't match", file=sys.stderr)
            return 1
        user = create_first_admin(email, password, account_name=args.account_name)
    except (SetupError, AccountError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"✗ Can't reach the control database: {first}", file=sys.stderr)
        return 1
    where = env.get("APP_HOST")
    print(f"✓ {user.email} is this Lumiverb's admin" + (f". Log in at {where}" if where else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumiverb-server", description="Look after this machine's Lumiverb server.")
    _env_option(parser, None)
    commands = parser.add_subparsers(dest="command", required=True)
    # --env before or after the command. A subcommand's default would overwrite
    # one given before it, so it sets --env only when given there.
    init_command = commands.add_parser(
        "init", help="write the install's env file, Quickwit's config and the nginx site, each only if it isn't there")
    _env_option(init_command, argparse.SUPPRESS)
    init_command.add_argument("--data-dir", type=Path, help="where its data goes (a new env file needs it)")
    init_command.add_argument("--quickwit-config", type=Path, help="write Quickwit's config here")
    init_command.add_argument("--nginx-site", type=Path, help="write the nginx site here")
    init_command.add_argument("--web-root", type=Path, default=WEB_ROOT, help="the web build nginx serves")
    for port in ("pg", "api", "quickwit", "web"):
        init_command.add_argument(f"--{port}-port", type=int, help="for a new env file")
    init_command.add_argument("--app-host", help="the URL people reach it at, for a new env file")
    _env_option(commands.add_parser("migrate", help="migrate the control plane and every account's database"),
                argparse.SUPPRESS)
    _env_option(commands.add_parser(
        "run", help="migrate, then run the API, the scheduler and upkeep until stopped (a Mac's service)"),
        argparse.SUPPRESS)
    setup_command = commands.add_parser("setup", help="make the install's first admin (while nobody can log in)")
    _env_option(setup_command, argparse.SUPPRESS)
    setup_command.add_argument("--email", help="the admin's email (asked if not given)")
    setup_command.add_argument("--account-name", help="a new account's name (\"Lumiverb\" if not given)")
    args = parser.parse_args(argv)
    try:
        return {"init": _init, "migrate": _migrate, "run": _run, "setup": _setup}[args.command](args)
    except (InstallError, EnvFileError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


def entry() -> None:
    sys.exit(main())
