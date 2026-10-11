"""lumiverb-server: look after this machine's Lumiverb server.

    lumiverb-server init --env FILE --data-dir DIR [--pg-bin DIR] [--quickwit-bin FILE --quickwit-config FILE]
                         [--nginx-bin FILE --nginx-config FILE] [--local-roots] [--reset-on-this-machine]
    lumiverb-server migrate --env FILE
    lumiverb-server run --env FILE
    lumiverb-server setup --env FILE [--email EMAIL] [--account-name NAME]
    lumiverb-server reset-password --env FILE [--email EMAIL]

init: the install's env file, Quickwit's config and nginx's, each written
only if it isn't there (init.py), for Homebrew's post_install.

migrate: the control plane's and every account's database migrations, as
scripts/migrate.sh does on the brain.

run: the whole server as one long-running service, for a Mac (Homebrew's
one service; launchd keeps it up). It starts what the env file names
(PG_BIN: Lumiverb's own PostgreSQL, made on first start; QUICKWIT_BIN;
NGINX_BIN), makes the control database if it's missing and migrates,
waiting for PostgreSQL, then runs the API and the scheduler as the brain's
systemd units do (supervise.py) and posts upkeep as its timers do
(upkeep.py), until SIGTERM. When `brew upgrade` replaces its install, it
stops everything and starts the new one, so an upgrade needs no restart.
The brain keeps its systemd units: it doesn't use this.

setup: the install's first admin, asking for their email and password; the
same as the web app's setup page (src/server/setup.py), while nobody can
log in yet.

reset-password: someone's new password, asked for twice, signing them out
everywhere: what Forgot password on a Mac itself does (no email needed), on
any install. The env file is the operator's, so this needs no other proof.

Which install is always said, never assumed: --env FILE, or
$LUMIVERB_ENV_FILE, or CONTROL_PLANE_DATABASE_URL already in the
environment (a dev checkout). On the brain /etc/lumiverb/env is prod's, so a
dev checkout run without one would migrate prod: it's refused instead. A
variable already in the environment wins over the file's, as for the
server's own settings.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from getpass import getpass
from pathlib import Path

from src.server.lifecycle import network, upkeep
from src.server.lifecycle.envfile import EnvFileError, read_env_file
from src.server.lifecycle.init import WEB_ROOT, Choices, InitError, init
from src.server.lifecycle.migrate import (
    APP_DIR,
    MigrationError,
    create_database_if_missing,
    migrate,
)
from src.server.lifecycle.postgres import (
    PostgresError,
    make_cluster_if_missing,
    postgres_argv,
    postgres_env,
)
from src.server.lifecycle.supervise import Child, Supervisor

logger = logging.getLogger("lumiverb-server")

NO_INSTALL = ("Say which install: --env FILE or $LUMIVERB_ENV_FILE, or CONTROL_PLANE_DATABASE_URL in the "
              "environment. None is assumed (on the brain, /etc/lumiverb/env is prod's).")
DEFAULT_API_PORT = "8000"  # update.sh's default when the env file has none
DEFAULT_API_WORKERS = "2"  # the brain's unit: uvicorn --workers 2
MIGRATE_RETRY_MAX_SEC = 60.0
UPGRADE_CHECK_SEC = 10.0


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


def server_children(env: dict[str, str], *, migrated: threading.Event | None = None, python: str = sys.executable,
                    cwd: Path = APP_DIR, config_dir: Path | None = None) -> list[Child]:
    """What the env file names to run (PostgreSQL, Quickwit, nginx), then the
    API and the scheduler (once `migrated`), with the brain's units'
    environment, restart delays and stop times (deploy-api.sh). They stop in
    reverse: the scheduler first, PostgreSQL last. The scheduler never reads
    the install's own settings (config_dir: the env file's folder, its secrets)."""
    base = {**env, "PYTHONUNBUFFERED": "1"}
    data = data_dir(env)
    started_first: list[Child] = []
    if env.get("PG_BIN"):
        # Stopped last, once the API and the scheduler, its clients, have gone:
        # a fast shutdown (SIGINT), the postmaster stopping its own backends.
        started_first.append(Child("postgres", postgres_argv(Path(env["PG_BIN"]), data,
                                                             env["CONTROL_PLANE_DATABASE_URL"]),
                                   postgres_env(base), cwd, restart_after=5, stop_within=60, term_main_only=True,
                                   stop_signal=signal.SIGINT))
    if env.get("QUICKWIT_BIN"):
        started_first.append(Child("quickwit", [env["QUICKWIT_BIN"], "run", "--config", env["QUICKWIT_CONFIG"]],
                                   base, cwd, restart_after=5, stop_within=30))
    if env.get("NGINX_BIN"):
        started_first.append(Child("nginx", [env["NGINX_BIN"], "-p", f"{data / 'nginx'}/",
                                             "-c", env["NGINX_CONFIG"], "-e", "stderr"],
                                   base, cwd, restart_after=5, stop_within=15, term_main_only=True))
    api = Child(
        "api",
        [python, "-m", "uvicorn", "src.server.api.main:app",
         "--host", env.get("API_LISTEN_HOST") or "127.0.0.1",
         "--port", env.get("API_PORT") or DEFAULT_API_PORT,
         "--workers", env.get("LUMIVERB_API_WORKERS") or DEFAULT_API_WORKERS],
        # Large uploads (analysis proxies) are spooled on the data disk.
        {**base, "TMPDIR": str(data / "tmp")}, cwd,
        restart_after=5, stop_within=90, empty_first=data / "tmp", start_after=migrated,
    )
    # Which library folders it reads is the env file's to say (LUMIVERB_ROOT_MAP),
    # never assumed here. What it refuses can only grow: the install's settings too.
    try:
        listed = json.loads(env.get("LUMIVERB_REFUSED_ROOTS") or "[]")
    except ValueError:
        listed = None
    if not isinstance(listed, list) or not all(isinstance(path, str) for path in listed):
        raise InstallError('LUMIVERB_REFUSED_ROOTS must be a JSON list of folders, e.g. \'["/Volumes/private"]\'')
    refused = [*listed, *([str(config_dir)] if config_dir else [])]
    scheduler = Child(
        "scheduler",
        [python, "-m", "src.server.scheduler"],
        {**base, "LUMIVERB_REFUSED_ROOTS": json.dumps(refused), "XDG_CACHE_HOME": str(data / "cache"),
         "TMPDIR": str(data / "worker-tmp")}, cwd,
        # It lets jobs in hand finish for up to 25 s; only it hears the stop.
        restart_after=30, stop_within=40, term_main_only=True, empty_first=data / "worker-tmp",
        start_after=migrated,
    )
    return [*started_first, api, scheduler]


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


def missing_settings(env: dict[str, str]) -> list[str]:
    """The server's required settings (src/server/config.py) the install
    doesn't set: without them the API and the scheduler fail at their first
    use, over and over."""
    from src.server.config import Settings

    return [name.upper() for name, field in Settings.model_fields.items()
            if field.is_required() and not env.get(name.upper())]


def _env_and_url(args: argparse.Namespace, *, server: bool = False) -> tuple[dict[str, str], str]:
    """The install's env and control database URL; for the server (run,
    setup), also every setting it requires."""
    env = install_env(args.env)
    where = args.env or os.environ.get("LUMIVERB_ENV_FILE") or "the environment"
    url = env.get("CONTROL_PLANE_DATABASE_URL")
    if not url:
        raise InstallError(f"No CONTROL_PLANE_DATABASE_URL in {where}")
    if server and (missing := missing_settings(env)):
        raise InstallError(f"{where} needs {', '.join(missing)}")
    return env, url


def _init(args: argparse.Namespace) -> int:
    env_file = _named_env_file(args.env)
    if env_file is None:
        raise InstallError("Say which env file: --env FILE or $LUMIVERB_ENV_FILE")
    def full(path: Path | None) -> Path | None:
        return path.absolute() if path else None

    choices = Choices(data_dir=full(args.data_dir), pg_port=args.pg_port, api_port=args.api_port,
                      quickwit_port=args.quickwit_port, web_port=args.web_port, app_host=args.app_host,
                      pg_bin=full(args.pg_bin), quickwit_bin=full(args.quickwit_bin),
                      quickwit_config=full(args.quickwit_config), nginx_bin=full(args.nginx_bin),
                      nginx_config=full(args.nginx_config), local_roots=args.local_roots,
                      reset_on_this_machine=args.reset_on_this_machine)
    if (choices.quickwit_bin is None) != (choices.quickwit_config is None) or (
            (choices.nginx_bin is None) != (choices.nginx_config is None)):
        raise InstallError("Name each program with its config: --quickwit-bin with --quickwit-config, "
                           "--nginx-bin with --nginx-config")
    try:
        init(env_file, choices, web_root=args.web_root)
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


def _then_migrated(env: dict[str, str], url: str, stop: threading.Event, migrated: threading.Event) -> None:
    """Migrations (waiting for PostgreSQL), then the API and the scheduler may
    start, and upkeep runs."""
    first_wait = 1.0 if env.get("PG_BIN") else 5.0  # ours is up in a second or two
    if not migrate_when_ready(url, stop, migrate_fn=_created_and_migrated, first_wait=first_wait):
        return
    migrated.set()
    base_url = f"http://127.0.0.1:{env.get('API_PORT') or DEFAULT_API_PORT}"
    if env.get("ADMIN_KEY"):
        schedule = upkeep.Schedule(datetime.now(), data_dir(env) / "upkeep-state.json")
        upkeep.run_upkeep(schedule, stop, is_healthy=lambda: upkeep.healthy(base_url),
                          send=lambda path: upkeep.post(base_url, path, env["ADMIN_KEY"]))
    else:
        logger.warning("No ADMIN_KEY: upkeep (search sync, trash purge, cleanup) won't run")


def install_identity(command: Path) -> tuple[str, int] | None:
    """Which install `command` is now: what it resolves to through Homebrew's
    opt link, and that file's inode (a reinstall of the same version makes a
    new one). None while it doesn't resolve (mid-upgrade)."""
    try:
        real = command.resolve(strict=True)
        return str(real), real.stat().st_ino
    except OSError:
        return None


def watch_for_upgrade(command: Path, stop: threading.Event, upgraded: threading.Event,
                      every: float = UPGRADE_CHECK_SEC) -> None:
    """Sets `upgraded` and `stop` once `command` is another install (`brew
    upgrade` moved the opt link): run then stops everything and starts the new
    one. Homebrew deletes the old install right after, so it's looked at often."""
    first = install_identity(command)
    if first is None:
        return
    while not stop.wait(every):
        now = install_identity(command)
        if now is not None and now != first:
            logger.info("Lumiverb was upgraded (%s): restarting into it", now[0])
            upgraded.set()
            stop.set()
            return


def _launched_as() -> Path:
    """The command this was started as: for Homebrew's service, through the opt link."""
    return Path(shutil.which(sys.argv[0]) or sys.argv[0]).absolute()


def _run(args: argparse.Namespace) -> int:
    env, url = _env_and_url(args, server=True)
    data = data_dir(env)  # before anything starts
    migrated = threading.Event()
    named = _named_env_file(args.env)
    # Built first: a setting it can't use stops it before anything starts.
    children = server_children(env, migrated=migrated, config_dir=named.absolute().parent if named else None)
    logging.basicConfig(level=logging.INFO, format="lumiverb-server: %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())

    if env.get("PG_BIN"):
        try:
            if make_cluster_if_missing(Path(env["PG_BIN"]), data, url):
                logger.info("Made Lumiverb's database in %s", data / "postgres")
        except PostgresError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
    if env.get("NGINX_BIN"):
        (data / "nginx").mkdir(parents=True, exist_ok=True)
    # Its own nginx listens where Settings › Network says: on this machine only
    # until an admin says other devices may use it.
    web_port = env.get("WEB_PORT", "")
    listens = network.managed(data, env.get("NGINX_CONFIG")) and web_port.isdigit()
    if listens:
        network.write_listen_file(data, int(web_port), network.other_devices(data))
    supervisor = Supervisor(children)
    if listens:
        threading.Thread(target=network.watch, args=(data, int(web_port), stop, lambda: supervisor.restart_soon("nginx")),
                         name="network-watch", daemon=True).start()
    threading.Thread(target=_then_migrated, args=(env, url, stop, migrated), name="migrate-then-upkeep",
                     daemon=True).start()
    launched, upgraded = _launched_as(), threading.Event()
    threading.Thread(target=watch_for_upgrade, args=(launched, stop, upgraded), kwargs={"every": UPGRADE_CHECK_SEC},
                     name="upgrade-watch", daemon=True).start()
    supervisor.run(stop)
    if upgraded.is_set():
        # The same process (launchd's job stays up), now the new install's code.
        os.execv(launched, [str(launched), *sys.argv[1:]])
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

    env, _ = _env_and_url(args, server=True)
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


def _reset_password(args: argparse.Namespace) -> int:
    import bcrypt
    from sqlalchemy.exc import SQLAlchemyError

    from src.server.api.routers.auth import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH
    from src.server.config import get_settings
    from src.server.database import get_control_session
    from src.server.repository.control_plane import UserRepository

    env, _ = _env_and_url(args)
    os.environ.update(env)  # the server's settings are the environment's (src/server/config.py)
    get_settings.cache_clear()
    email = (args.email or input("Email: ")).strip()
    try:
        with get_control_session() as session:
            user = UserRepository(session).get_by_email(email)
        if user is None:
            print("✗ There's no account with that email here", file=sys.stderr)
            return 1
        password = getpass("New password: ")
        if len(password) < MIN_PASSWORD_LENGTH or len(password.encode()) > MAX_PASSWORD_BYTES:
            print(f"✗ Use {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_BYTES} characters", file=sys.stderr)
            return 1
        if getpass("New password again: ") != password:
            print("✗ The passwords don't match", file=sys.stderr)
            return 1
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
        with get_control_session() as session:
            UserRepository(session).update_password(user.user_id, password_hash)
    except SQLAlchemyError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"✗ Can't reach the control database: {first}", file=sys.stderr)
        return 1
    print(f"✓ A new password for {email}; they're signed out everywhere else")
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
    init_command.add_argument("--pg-bin", type=Path, help="PostgreSQL's bin folder: run starts Lumiverb's own")
    init_command.add_argument("--quickwit-bin", type=Path, help="Quickwit: run starts it")
    init_command.add_argument("--quickwit-config", type=Path, help="Quickwit's config, written here")
    init_command.add_argument("--nginx-bin", type=Path, help="nginx: run starts it")
    init_command.add_argument("--nginx-config", type=Path, help="nginx's config, written here")
    init_command.add_argument("--local-roots", action="store_true",
                              help="the scheduler reads every library folder where it is (this machine has the files)")
    init_command.add_argument("--reset-on-this-machine", action="store_true",
                              help="Forgot password on this machine gives the reset link at once: whoever is at it "
                                   "owns the install (a Mac's, never a server's)")
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
    reset_command = commands.add_parser(
        "reset-password", help="set someone's new password, signing them out everywhere (no email needed)")
    _env_option(reset_command, argparse.SUPPRESS)
    reset_command.add_argument("--email", help="theirs (asked if not given)")
    args = parser.parse_args(argv)
    try:
        return {"init": _init, "migrate": _migrate, "run": _run, "setup": _setup,
                "reset-password": _reset_password}[args.command](args)
    except (InstallError, EnvFileError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


def entry() -> None:
    sys.exit(main())
