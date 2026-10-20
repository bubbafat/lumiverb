"""lumiverb-server: look after this machine's Lumiverb server.

    lumiverb-server init --env FILE --data-dir DIR [--pg-bin DIR] [--nginx-bin FILE --nginx-config FILE]
                         [--local-roots] [--reset-on-this-machine]
    lumiverb-server migrate --env FILE
    lumiverb-server run --env FILE
    lumiverb-server setup --env FILE [--email EMAIL] [--account-name NAME]
    lumiverb-server reset-password --env FILE [--email EMAIL]
    lumiverb-server backup --env FILE [--before-upgrade]
    lumiverb-server restore --env FILE BACKUP [--yes]
    lumiverb-server ai-worker --env FILE [--host HOST] [--port PORT] [--at-once N] [--show-key | --install | --uninstall]

init: the install's env file and nginx's config, each written only if it
isn't there (init.py), for Homebrew's post_install.

migrate: the control plane's and every account's database migrations, as
scripts/migrate.sh does on Linux.

run: the whole server as one long-running service, for a Mac (Homebrew's
one service; launchd keeps it up). It starts what the env file names
(PG_BIN: Lumiverb's own PostgreSQL, made on first start; NGINX_BIN), makes
the control database if it's missing and migrates,
waiting for PostgreSQL, then runs the API and the scheduler as a Linux
install's systemd units do (supervise.py) and posts upkeep as its timers do
(upkeep.py), until SIGTERM. When `brew upgrade` replaces its install, it
stops everything and starts the new one, so an upgrade needs no restart.
A Linux install keeps its systemd units: it doesn't use this. Quickwit is
never started (ADR-019: search is in PostgreSQL), though an older env file
names it; what it left (its index, the config init wrote) goes at start
(forget_quickwit).

setup: the install's first admin, asking for their email and password; the
same as the web app's setup page (src/server/setup.py), while nobody can
log in yet.

reset-password: someone's new password, asked for twice, signing them out
everywhere and revoking the API keys they made, and voiding their unused
reset and invite links: what Forgot password on a Mac itself does (no email
needed), on any install. The env file is the operator's, so this needs no
other proof.

backup: every database backed up now, into the install's backups folder
(src/server/backups.py), as Settings › Backups' Back up now does. Run as
root (a Linux install's sudo), backup and restore go on as the data
folder's owner, so what they write stays the API's. --before-upgrade marks
it as an upgrade's (a Linux update runs it before migrating; the 3 newest
of those are kept however old).

restore: every database put back from a backup (its folder, or its name in
the backups folder), with Lumiverb stopped: after backing up what's there
first, so it can be undone. Then migrated to this version; search is in the
databases put back. A Mac's own PostgreSQL is started for
the while if the service is stopped.

ai-worker: CLIP and face detection for another machine's scheduler, as an
AI machine in its Settings › AI (ADR-022, src/server/ai_worker/): the
install's models, no account, database or files; on every interface, port
6366, by default. Its key is made on first start, kept in the data folder
(owner-only), and printed by --show-key, to paste into Settings › AI.
--install runs it as a Mac's service (a launchd agent of its own:
src/server/ai_worker/service.py), --uninstall takes that away; after `brew
upgrade` it restarts into the new install, as run does.

Which install is always said, never assumed: --env FILE, or
$LUMIVERB_ENV_FILE, or CONTROL_PLANE_DATABASE_URL already in the
environment (a dev checkout). On Linux /etc/lumiverb/env is the live
install's, so a dev checkout run without one would migrate it: it's refused
instead. A variable already in the environment wins over the file's, as for
the server's own settings.
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

from src.server import backups
from src.server.ai_worker import DEFAULT_PORT as DEFAULT_AI_WORKER_PORT
from src.server.lifecycle import api_port, log, network, plugins_cli, private, upkeep
from src.server.lifecycle.envfile import EnvFileError, read_env_file
from src.server.lifecycle.init import WEB_ROOT, Choices, InitError, init
from src.server.lifecycle.migrate import (
    APP_DIR,
    MigrationError,
    create_database_if_missing,
    migrate,
    pending,
)
from src.server.lifecycle.postgres import (
    PostgresError,
    make_cluster_if_missing,
    postgres_argv,
    postgres_env,
    up_while,
)
from src.server.lifecycle.supervise import Child, Supervisor
from src.server.lifecycle import roles
from src.shared.secrets_env import without_secrets
from src.shared.stop_signals import set_on_signals

logger = logging.getLogger("lumiverb-server")

NO_INSTALL = ("Say which install: --env FILE or $LUMIVERB_ENV_FILE, or CONTROL_PLANE_DATABASE_URL in the "
              "environment. None is assumed (on Linux, /etc/lumiverb/env is the live install's).")
DEFAULT_API_PORT = "8000"  # update.sh's default when the env file has none
DEFAULT_API_WORKERS = "2"  # a Linux install's unit: uvicorn --workers 2
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


# The scheduler reads the database directly and makes its own API keys
# (scheduler/service.py scheduler_key): no other secret. The server's
# settings require the template (config.py) though it reads each account's
# database from its routing: the same credentials as the control URL's.
SCHEDULER_SECRETS = ("CONTROL_PLANE_DATABASE_URL", "TENANT_DATABASE_URL_TEMPLATE")


def forget_quickwit(env: dict[str, str], data: Path) -> list[Path]:
    """What Quickwit left on an install made before search moved into
    PostgreSQL (ADR-019): its index (DATA_DIR/quickwit) and the config init
    wrote for it (the env file's QUICKWIT_CONFIG, only if it's that one).
    Nothing reads either now. Removed, each said in the log; returns what was."""
    gone: list[Path] = []
    index = data / "quickwit"
    if index.is_dir() and not index.is_symlink():
        shutil.rmtree(index, ignore_errors=True)
        if index.exists():
            logger.warning("Couldn't remove all of %s (Quickwit's index, not used any more)", index)
        else:
            gone.append(index)
    config = Path(env["QUICKWIT_CONFIG"]) if env.get("QUICKWIT_CONFIG") else None
    if config is not None and config.is_file() and not config.is_symlink():
        try:
            written_by_init = "node_id: lumiverb" in config.read_text(errors="replace")
        except OSError:
            written_by_init = False
        if written_by_init:
            config.unlink(missing_ok=True)
            gone.append(config)
    for path in gone:
        logger.info("Removed %s: Quickwit isn't used any more (search is in PostgreSQL)", path)
    if gone and any(key.startswith("QUICKWIT_") for key in env):
        logger.info("The env file's QUICKWIT_* lines aren't read any more: they may be removed")
    return gone


def server_children(env: dict[str, str], *, migrated: threading.Event | None = None, python: str = sys.executable,
                    cwd: Path = APP_DIR, config_dir: Path | None = None, env_file: Path | None = None,
                    log_file: Path | None = None) -> list[Child]:
    """What the env file names to run (PostgreSQL, nginx), then the
    API, the scheduler and the plug-in runner (once `migrated`), with a Linux
    install's units' environment, restart delays and stop times
    (deploy-api.sh). They stop in reverse: the plug-ins first, PostgreSQL last. The scheduler never reads
    the install's own settings (config_dir: the env file's folder, its secrets).
    The API is told which env file is the install's (env_file), to say how to
    restore a backup, and where the service's log is (log_file: launchd's, for
    Admin › Logs). Each gets only the secrets it needs (secrets_env): the API
    all of them but the database superuser's (run's alone: roles.py), the
    scheduler the control database's URL, the rest none."""
    base = {**{k: v for k, v in env.items() if k != roles.SUPERUSER_URL}, "PYTHONUNBUFFERED": "1"}
    no_secrets = without_secrets(base)
    data = data_dir(env)
    started_first: list[Child] = []
    if env.get("PG_BIN"):
        # Stopped last, once the API and the scheduler, its clients, have gone:
        # a fast shutdown (SIGINT), the postmaster stopping its own backends.
        started_first.append(Child("postgres", postgres_argv(Path(env["PG_BIN"]), data,
                                                             env["CONTROL_PLANE_DATABASE_URL"]),
                                   postgres_env(no_secrets), cwd, restart_after=5, stop_within=60, term_main_only=True,
                                   stop_signal=signal.SIGINT))
    if env.get("NGINX_BIN"):
        started_first.append(Child("nginx", [env["NGINX_BIN"], "-p", f"{data / 'nginx'}/",
                                             "-c", env["NGINX_CONFIG"], "-e", "stderr"],
                                   no_secrets, cwd, restart_after=5, stop_within=15, term_main_only=True))
    api = Child(
        "api",
        # No line per request (over half the log, every thumbnail): nginx
        # answers the web, and the API logs what goes wrong.
        [python, "-m", "uvicorn", "src.server.api.main:app", "--no-access-log",
         "--host", env.get("API_LISTEN_HOST") or "127.0.0.1",
         "--port", env.get("API_PORT") or DEFAULT_API_PORT,
         "--workers", env.get("LUMIVERB_API_WORKERS") or DEFAULT_API_WORKERS],
        # Large uploads (analysis proxies) are spooled on the data disk.
        {**base, "TMPDIR": str(data / "tmp"), **({"LUMIVERB_ENV_FILE": str(env_file)} if env_file else {}),
         **({"LUMIVERB_LOG_FILE": str(log_file)} if log_file else {})}, cwd,
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
        {**without_secrets(base, keep=SCHEDULER_SECRETS), "LUMIVERB_REFUSED_ROOTS": json.dumps(refused),
         "XDG_CACHE_HOME": str(data / "cache"),
         "TMPDIR": str(data / "worker-tmp")}, cwd,
        # It lets jobs in hand finish for up to 25 s; only it hears the stop.
        restart_after=30, stop_within=40, term_main_only=True, empty_first=data / "worker-tmp",
        start_after=migrated,
    )
    # Every installed plug-in, a process of its own (ADR-023): the runner reads what's installed from the
    # control database, and what it starts gets none of Lumiverb's settings. It stops them within 10 s.
    plugins = Child(
        "plugins",
        [python, "-m", "src.server.plugin_runner"],
        without_secrets(base, keep=SCHEDULER_SECRETS), cwd,
        restart_after=10, stop_within=20, term_main_only=True, start_after=migrated,
    )
    return [*started_first, api, scheduler, plugins]


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
                      web_port=args.web_port, app_host=args.app_host, pg_bin=full(args.pg_bin),
                      nginx_bin=full(args.nginx_bin), nginx_config=full(args.nginx_config),
                      local_roots=args.local_roots, reset_on_this_machine=args.reset_on_this_machine)
    if (choices.nginx_bin is None) != (choices.nginx_config is None):
        raise InstallError("Name nginx with its config: --nginx-bin with --nginx-config")
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
    env, url = _env_and_url(args)
    if getattr(args, "to", None) or getattr(args, "control_to", None):
        return _migrate_to(args, env, url)
    try:
        count = migrate(url)
    except MigrationError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ Migrated the control plane and {count} account(s)")
    _install_own(env, url, say=print, fail=lambda line: print(line, file=sys.stderr))
    return 0


def _migrate_to(args: argparse.Namespace, env: dict[str, str], url: str) -> int:
    """`migrate --to`: the databases taken to the revisions named, down or up (migrate.migrate_to). Refused
    while Lumiverb runs (connected to its databases, as a restore is refused: its API and scheduler would go on
    writing what the newer code writes, and a Mac's `run` would migrate up again at its next start); a Mac's own
    PostgreSQL started for the while, as backup and restore do."""
    from src.server import backups
    from src.server.lifecycle.migrate import migrate_to, tenant_databases

    data = data_dir(env)
    pg_bin = env.get("PG_BIN") or None
    try:
        with up_while(Path(pg_bin) if pg_bin else None, data, url, env):
            busy = backups.connected_elsewhere([url, *(u for _, u in tenant_databases(url))])
            if busy:
                print("✗ Lumiverb is still running (connected: " + ", ".join(busy) + "). Stop it first:"
                      " brew services stop lumiverb, or its systemd services.", file=sys.stderr)
                return 1
            moved = migrate_to(url, tenant=args.to, control=args.control_to)
    except (MigrationError, PostgresError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ {moved} account(s) at {args.to}" if args.to else "✓ The control plane moved")
    return 0


def _install_own(env: dict[str, str], url: str, *, say, fail) -> None:
    """Migrating's last step: Lumiverb's own plug-ins installed or updated (src/server/own_plugins.py), never
    stopping what follows."""
    from src.server.own_plugins import install_own

    try:
        data: Path | None = data_dir(env)
    except InstallError:
        data = None
    install_own(url, data, environ=env, say=say, fail=fail)


def backed_up_before_upgrade(env: dict[str, str], url: str) -> backups.Backup | None:
    """A backup before migrations this version brings (an upgrade's), into
    the backups folder, else DATA_DIR/backups (the chosen one may be a drive
    that isn't plugged in). One that can't be made is logged, and the
    upgrade goes on: the nightly ones are there, and a service that won't
    start after an upgrade is worse. None when nothing needs migrating."""
    if not pending(url):
        return None
    data, pg_bin = data_dir(env), env.get("PG_BIN") or None
    tried = []
    for dest in dict.fromkeys([backups.folder(data), backups.default_folder(data)]):
        try:
            backup = backups.back_up(data, url, "before-upgrade", pg_bin=pg_bin, dest=dest)
            logger.info("Backed up before migrating: %s", backup.path)
            return backup
        except backups.BackupError as exc:
            tried.append(f"{dest}: {exc}")
    logger.error("No backup before migrating (%s); migrating anyway", "; ".join(tried))
    return None


def _created_and_migrated(url: str, env: dict[str, str] | None = None) -> int:
    """Lumiverb's own PostgreSQL's databases handed to its ordinary role
    (roles.py, as the superuser: every start, nothing done when nothing
    needs it), then, as that role, its control database made if missing,
    backed up if an upgrade's migrations are waiting, then migrated. Said
    through the log: print()'s lines wait in a buffer when the log is a
    file (launchd's)."""
    if env is not None and env.get(roles.SUPERUSER_URL):
        roles.ensure_app_role(env[roles.SUPERUSER_URL], url)
    if create_database_if_missing(url):
        logger.info("Created the control database")
    if env is not None:
        backed_up_before_upgrade(env, url)
    count = migrate(url, say=logger.info)
    if env is not None:
        _install_own(env, url, say=logger.info, fail=logger.error)
    return count


def _then_migrated(env: dict[str, str], url: str, stop: threading.Event, migrated: threading.Event) -> None:
    """Migrations (waiting for PostgreSQL), then the API and the scheduler may
    start, and upkeep runs."""
    first_wait = 1.0 if env.get("PG_BIN") else 5.0  # ours is up in a second or two
    if not migrate_when_ready(url, stop, migrate_fn=lambda u: _created_and_migrated(u, env), first_wait=first_wait):
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


def as_app_role(env: dict[str, str], env_file: Path | None) -> dict[str, str]:
    """The env, its databases as Lumiverb's ordinary role's (roles.py), for
    an install whose PostgreSQL is run's own (PG_BIN, a Mac's): the env file
    moved over once. A Linux install's role is already one (deploy-api.sh)."""
    if not env.get("PG_BIN") or env_file is None:
        return env
    return roles.with_app_role(env_file, env)


def _run(args: argparse.Namespace) -> int:
    env, url = _env_and_url(args, server=True)
    named = _named_env_file(args.env)
    before = env.get(roles.SUPERUSER_URL)
    env = as_app_role(env, named)  # before the children are built: they connect as its role
    ports_moved = None
    if named is not None:  # the API off 6465, HTTPS's now (api_port.py), before anything starts on either
        env, ports_moved = api_port.moved(named, env)
    url = env["CONTROL_PLANE_DATABASE_URL"]
    data = data_dir(env)  # before anything starts
    migrated = threading.Event()
    log_file = log.file_of(2)
    # Built first: a setting it can't use stops it before anything starts.
    children = server_children(env, migrated=migrated, config_dir=named.absolute().parent if named else None,
                               env_file=named.absolute() if named else None, log_file=log_file)
    log.configure()  # launchd's file, rotated, each child's lines named (log.py)
    if env.get(roles.SUPERUSER_URL) and not before:
        logger.info("Database: %s now connects as %s, the superuser's URL kept as %s (the old file beside it)",
                    named, roles.APP_ROLE, roles.SUPERUSER_URL)
    if ports_moved:
        logger.info("Ports: %s", ports_moved)
    private.begin(data, log_file)  # umask 077 before anything is made, and what's there made private
    forget_quickwit(env, data)
    stop = threading.Event()
    set_on_signals(stop, (signal.SIGTERM, signal.SIGINT, signal.SIGHUP))

    if env.get("PG_BIN"):
        try:
            if make_cluster_if_missing(Path(env["PG_BIN"]), data, env.get(roles.SUPERUSER_URL) or url):
                logger.info("Made Lumiverb's database in %s", data / "postgres")
        except PostgresError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
    if env.get("NGINX_BIN"):
        (data / "nginx").mkdir(parents=True, exist_ok=True)
    # Its own nginx listens where Settings › Network says: on this machine only
    # until an admin says other devices may use it (over HTTP, and HTTPS if they
    # add it), on its ports (the env file's WEB_PORT and WEB_TLS_PORT unless an
    # admin chose others).
    web_port = env.get("WEB_PORT", "")
    listens = network.managed(data, env.get("NGINX_CONFIG")) and web_port.isdigit()
    defaults = network.default_ports(web_port, env.get("WEB_TLS_PORT"))
    if listens:
        if (at := network.ports(data, defaults)) != defaults and (why := network.port_problem(env, at)):
            logger.warning("Network: %s: back to ports %d and %d", why, defaults.http, defaults.https)
            network.set_ports(data, defaults)
            at = defaults
        https = network.choice(data).https_for_others
        for name, port in (("WEB_PORT", at.http), *((("WEB_TLS_PORT", at.https),) if https else ())):
            if taken := network.port_taken(env, port):
                print(f"✗ {name} {port} is also {taken}: set another in {named or 'the env file'}", file=sys.stderr)
                return 1
        try:  # the certificate other devices are served over HTTPS (tls.py): made, or renewed, now
            network.apply(data, defaults)
        except Exception:  # noqa: BLE001 — served over plain HTTP meanwhile; the watch tries again
            logger.exception("Network: couldn't apply the choice or the certificate")
            network.without_https(data, defaults)
    supervisor = Supervisor(children, relay=log.relay)
    if listens:
        threading.Thread(target=network.watch, args=(data, defaults, stop, lambda: supervisor.restart_soon("nginx")),
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
            revoked = UserRepository(session).update_password(user.user_id, password_hash)
    except SQLAlchemyError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"✗ Can't reach the control database: {first}", file=sys.stderr)
        return 1
    keys = ("" if not revoked else ", and the API key they made is revoked" if revoked == 1
            else f", and the {revoked} API keys they made are revoked")
    print(f"✓ A new password for {email}; they're signed out everywhere else{keys}")
    return 0


def _server_admins(args: argparse.Namespace) -> int:
    """server-admins [add|remove EMAIL]: who looks after the install, its
    accounts and what they share (ADR-018 decision 8), then who does. On the
    machine itself, so it's the way to name one where there's none, and it
    may take away the last."""
    from sqlalchemy.exc import SQLAlchemyError

    from src.server.config import get_settings
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository, UserRepository

    if args.action is not None and not args.email:
        print(f"✗ {args.action.capitalize()} whom? Their email, as they sign in", file=sys.stderr)
        return 2
    env, _ = _env_and_url(args)
    os.environ.update(env)  # the server's settings are the environment's (src/server/config.py)
    get_settings.cache_clear()
    try:
        with get_control_session() as session:
            users = UserRepository(session)
            if args.action is not None:
                user = users.get_by_email(args.email.strip())
                if user is None:
                    print("✗ Nobody signs in here with that email", file=sys.stderr)
                    return 1
                users.set_server_admin(user.user_id, args.action == "add")
                print(f"✓ {user.email} is a server admin" if args.action == "add"
                      else f"✓ {user.email} is no longer a server admin")
            names = {t.tenant_id: t.name for t in TenantRepository(session).list_all()}
            admins = [(u.email, names.get(u.tenant_id, "")) for u in users.server_admins()]
    except SQLAlchemyError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"✗ Can't reach the control database: {first}", file=sys.stderr)
        return 1
    if not admins:
        print("This Lumiverb has no server admin: name one with `lumiverb-server server-admins add EMAIL`")
    else:
        print("Server admins:")
        for email, account in admins:
            print(f"  {email} ({account})")
    return 0


def _plugins(args: argparse.Namespace) -> int:
    """plugins [install|default|uninstall]: the plug-ins installed on this server (ADR-023), on the machine."""
    from src.server.config import get_settings
    from src.server.lifecycle.plugins_cli import run

    env, _ = _env_and_url(args)
    os.environ.update(env)  # the server's settings are the environment's (src/server/config.py)
    get_settings.cache_clear()
    return run(args, data_dir(env))


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def as_data_owner(data: Path) -> None:
    """Run on as whoever owns the data folder, when started as root (sudo, to
    read a Linux install's env file): what's written there (backups, their
    state) stays the API's to read, prune and update."""
    if os.geteuid() != 0:
        return
    try:
        owner = data.stat()
    except OSError:
        return
    if owner.st_uid == 0:
        return
    os.setgroups([])
    os.setgid(owner.st_gid)
    os.setuid(owner.st_uid)


def _backup(args: argparse.Namespace) -> int:
    env, url = _env_and_url(args)
    data = data_dir(env)
    as_data_owner(data)
    try:
        with up_while(Path(env["PG_BIN"]) if env.get("PG_BIN") else None, data, url, env):
            backup = backups.back_up(data, url, "before-upgrade" if args.before_upgrade else "now",
                                     pg_bin=env.get("PG_BIN") or None)
    except (backups.BackupError, PostgresError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ Backed up {len(backup.databases)} database(s), {_size(backup.bytes)}: {backup.path}")
    return 0


def _which_backup(name: str, data: Path) -> backups.Backup:
    """A backup by its folder, or its name in the backups folder."""
    path = Path(name).expanduser()
    if not path.is_absolute() and not path.exists():
        path = backups.folder(data) / name
    return backups.read(path.absolute())


def _restore(args: argparse.Namespace) -> int:
    env, url = _env_and_url(args)
    data = data_dir(env)
    as_data_owner(data)
    pg_bin = env.get("PG_BIN") or None
    try:
        backup = _which_backup(args.backup, data)
        backups.verify(backup)
    except backups.BackupError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    made = backup.made_at.astimezone().strftime("%b %-d %Y, %H:%M")
    kind = {"now": "made by hand", "before-upgrade": "made before an upgrade",
            "before-restore": "made before a restore"}.get(backup.kind, backup.kind)
    print(f"The backup from {made} ({kind}): the control plane and {backup.accounts} account(s).")
    print("Everything since then is replaced. What's here now is backed up first, so this can be undone.")
    if not args.yes and input("Type restore to go on: ").strip() != "restore":
        print("✗ Nothing was changed", file=sys.stderr)
        return 1
    try:
        with up_while(Path(pg_bin) if pg_bin else None, data, url, env):
            try:
                first = backups.back_up(data, url, "before-restore", pg_bin=pg_bin)
                print(f"✓ What was here: {first.path}")
            except backups.BackupError as exc:
                if not _has_database(url):
                    print("  Nothing here to back up first (a new install)")
                else:
                    raise backups.BackupError(f"Couldn't back up what's here first, so nothing was changed: {exc}") from None
            print("Putting back")
            left = backups.restore(backup, url, tenant_url_template=env.get("TENANT_DATABASE_URL_TEMPLATE") or None,
                                   pg_bin=pg_bin)
            print("Migrating to this version")
            migrate(url, say=lambda line: print(f"  {line}"))
    except (backups.BackupError, PostgresError, MigrationError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    for name in left:
        print(f"  ! {name} isn't in the backup: left as it was, no account lists it now")
    print(f"✓ Restored the backup from {made}. Start Lumiverb again.")
    return 0


_STATE_WORDS = {"running": "running", "partly": "partly paused", "paused": "paused"}


def _pausing(args: argparse.Namespace) -> int:
    """pause|resume: a switch (upkeep, a producer, an installed plug-in's work), every library's scans
    or one's, or all, as Settings › Processing and `lumiverb pause|resume`
    do (the same routes' code, without a key: on the machine itself), in
    every account or the one named. A producer by the name of what it makes,
    with or without `lumiverb.`, or its old word, warned of (ADR-024). Then
    each account's state."""
    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from src.producers.names import artifact_in, short, typed
    from src.server.api.errors import ApiError
    from src.server.api.routers import libraries as library_routes
    from src.server.api.routers import producers as producer_routes
    from src.server.config import get_settings
    from src.server.database import get_control_session, get_tenant_session
    from src.server.plugin_producers import refresh
    from src.server.repository import lineage
    from src.server.repository.control_plane import TenantRepository
    from src.shared.producers import PAUSE_ALL, PAUSE_SCANS, pause_state, pause_targets

    pausing = args.command == "pause"
    if args.library and args.name != PAUSE_SCANS:
        print("✗ --library goes with scans: a library's own Scans pause", file=sys.stderr)
        return 2
    env, _ = _env_and_url(args)
    os.environ.update(env)  # the server's settings are the environment's (src/server/config.py)
    get_settings.cache_clear()
    try:
        refresh()  # an installed plug-in's work is a producer too, as the API's requests read them (ADR-023)
        names = [PAUSE_ALL, PAUSE_SCANS, *pause_targets()]
        target, warning = typed(args.name, names)
        if target is None:
            print(f"✗ Nothing here is called {args.name}: {', '.join(short(artifact_in(n)) for n in names)}",
                  file=sys.stderr)
            return 2
        if warning:
            print(f"! {warning}", file=sys.stderr)
        with get_control_session() as ctrl:
            accounts = [(t.tenant_id, t.name) for t in TenantRepository(ctrl).list_all()
                        if args.account in (None, t.tenant_id, t.name)]
        if not accounts:
            print(f"✗ No account here is {args.account}" if args.account else "✗ This Lumiverb has no account yet",
                  file=sys.stderr)
            return 1
        acted = 0
        for tenant_id, name in accounts:
            with get_tenant_session(tenant_id) as session:
                if target == PAUSE_SCANS:
                    libraries = session.execute(text(
                        "SELECT library_id FROM libraries WHERE library_id = :l OR name = :l"),
                        {"l": args.library}).scalars().all() if args.library else None
                    if libraries == []:
                        continue  # no such library in this account
                    body = library_routes.ScansPauseRequest(library_ids=libraries, all=libraries is None)
                    if pausing:
                        library_routes.pause_scans(body, session, None)
                    else:
                        library_routes.resume_scans(body, session)
                else:
                    work = producer_routes.PauseIn(scope=lineage.WORK)
                    if pausing:
                        producer_routes.pause_switch(target, work, session, None)
                    else:
                        producer_routes.resume_switch(target, work, session)
                acted += 1
                held = lineage.pauses(session)
                active = session.execute(text("SELECT library_id FROM libraries WHERE status = 'active'")).scalars().all()
                state = pause_state(set(held.work), libraries=len(active),
                                    libraries_paused=sum(1 for lib in active if held.scans_paused(lib)))
                print(f"✓ {name}: processing {_STATE_WORDS[state]}")
    except (ApiError, HTTPException) as exc:
        print(f"✗ {getattr(exc, 'message', None) or exc.detail}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"✗ Can't reach the databases: {first}", file=sys.stderr)
        return 1
    if not acted:
        print(f"✗ No library here is {args.library}", file=sys.stderr)
        return 1
    return 0


def _has_database(url: str) -> bool:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False
    finally:
        engine.dispose()


def _ai_worker(args: argparse.Namespace) -> int:
    from src.server.ai_worker import ensure_key

    env = install_env(args.env)
    data = data_dir(env)
    key = ensure_key(data)
    if args.show_key:
        print(key)
        return 0
    if args.uninstall:
        from src.server.ai_worker.service import uninstall

        print("✓ The AI worker's service is stopped and gone" if uninstall() else "No AI worker service here")
        return 0
    if args.install:
        from src.server.ai_worker.service import install

        env_file = _named_env_file(args.env)
        if env_file is None:
            raise InstallError("--install needs the install's env file (--env FILE): the service names it")
        try:
            path = install(_launched_as(), env_file, data, host=args.host, port=args.port, at_once=args.at_once)
        except RuntimeError as exc:
            raise InstallError(str(exc)) from None
        print(f"✓ The AI worker runs as a service ({path}), on port {args.port}, and again at login.\n"
              f"  In the other Lumiverb's Settings › AI, add a machine at http://<this Mac's name>:{args.port}/v1\n"
              f"  with its key (lumiverb-server ai-worker --env {env_file} --show-key) doing CLIP and faces.")
        return 0
    os.environ.update(env)  # DATA_DIR: the models folder (src/processing/models_dir.py)
    import uvicorn

    from src.server.ai_worker.app import CLIP_MODELS, FACE_MODELS, make_app
    from src.server.scheduler.models import Models
    from src.shared.logging_config import configure_api_logging

    configure_api_logging()
    models = Models()
    stop = threading.Event()

    def let_go_of_idle() -> None:  # a model unused for a while frees its GPU memory, as in the scheduler
        while not stop.wait(60):
            models.let_go_of_idle()

    threading.Thread(target=let_go_of_idle, name="ai-worker-idle", daemon=True).start()
    # `brew upgrade` moving the opt link it was started through: it stops and starts again as the new install.
    launched, upgraded = _launched_as(), threading.Event()
    threading.Thread(target=watch_for_upgrade, args=(launched, stop, upgraded), name="upgrade-watch",
                     daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(make_app(models, key, at_once=args.at_once), host=args.host,
                                           port=args.port, access_log=False, log_config=None))

    def stop_serving() -> None:
        stop.wait()
        server.should_exit = True

    threading.Thread(target=stop_serving, name="ai-worker-stop", daemon=True).start()
    logger.info("Lumiverb AI worker on %s:%d, %d at once: CLIP %s, faces %s. Its key: lumiverb-server ai-worker "
                "--show-key", args.host, args.port, args.at_once, ", ".join(CLIP_MODELS), ", ".join(sorted(FACE_MODELS)))
    try:
        server.run()
    finally:
        stop.set()
        models.close()
    if upgraded.is_set():
        # The same process (launchd's agent stays up), now the new install's code.
        os.execv(launched, [str(launched), *sys.argv[1:]])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumiverb-server", description="Look after this machine's Lumiverb server.")
    _env_option(parser, None)
    commands = parser.add_subparsers(dest="command", required=True)
    # --env before or after the command. A subcommand's default would overwrite
    # one given before it, so it sets --env only when given there.
    init_command = commands.add_parser(
        "init", help="write the install's env file and the nginx site, each only if it isn't there")
    _env_option(init_command, argparse.SUPPRESS)
    init_command.add_argument("--data-dir", type=Path, help="where its data goes (a new env file needs it)")
    init_command.add_argument("--pg-bin", type=Path, help="PostgreSQL's bin folder: run starts Lumiverb's own")
    init_command.add_argument("--nginx-bin", type=Path, help="nginx: run starts it")
    init_command.add_argument("--nginx-config", type=Path, help="nginx's config, written here")
    init_command.add_argument("--local-roots", action="store_true",
                              help="the scheduler reads every library folder where it is (this machine has the files)")
    init_command.add_argument("--reset-on-this-machine", action="store_true",
                              help="Forgot password on this machine gives the reset link at once: whoever is at it "
                                   "owns the install (a Mac's, never a server's)")
    init_command.add_argument("--web-root", type=Path, default=WEB_ROOT, help="the web build nginx serves")
    for port in ("pg", "api", "web"):
        init_command.add_argument(f"--{port}-port", type=int, help="for a new env file")
    init_command.add_argument("--app-host", help="the URL people reach it at, for a new env file")
    migrate_command = commands.add_parser("migrate", help="migrate the control plane and every account's database")
    _env_option(migrate_command, argparse.SUPPRESS)
    migrate_command.add_argument(
        "--to", metavar="REVISION", help="every account's database to this revision instead, down or up: a rollback,"
        " with the service stopped, by the newer release (the release notes name the revision)")
    migrate_command.add_argument("--control-to", metavar="REVISION",
                                 help="the control plane's database to this revision too")
    _env_option(commands.add_parser(
        "run", help="migrate, then run the API, the scheduler and upkeep until stopped (a Mac's service)"),
        argparse.SUPPRESS)
    setup_command = commands.add_parser("setup", help="make the install's first admin (while nobody can log in)")
    _env_option(setup_command, argparse.SUPPRESS)
    setup_command.add_argument("--email", help="the admin's email (asked if not given)")
    setup_command.add_argument("--account-name", help="a new account's name (\"Lumiverb\" if not given)")
    reset_command = commands.add_parser(
        "reset-password",
        help="set someone's new password, signing them out everywhere and revoking their API keys (no email needed)")
    _env_option(reset_command, argparse.SUPPRESS)
    reset_command.add_argument("--email", help="theirs (asked if not given)")
    admins_command = commands.add_parser(
        "server-admins", help="who looks after this install (its accounts, network, backups, logs); add or remove one")
    _env_option(admins_command, argparse.SUPPRESS)
    admins_command.add_argument("action", nargs="?", choices=("add", "remove"), help="add or remove one")
    admins_command.add_argument("email", nargs="?", help="theirs, as they sign in")
    backup_command = commands.add_parser("backup", help="back up every database now, into the backups folder")
    _env_option(backup_command, argparse.SUPPRESS)
    backup_command.add_argument("--before-upgrade", action="store_true",
                                help="an upgrade's backup, before migrating (the 3 newest kept however old)")
    restore_command = commands.add_parser(
        "restore", help="put every database back from a backup, with Lumiverb stopped (what's there is backed up first)")
    _env_option(restore_command, argparse.SUPPRESS)
    restore_command.add_argument("backup", help="the backup's folder, or its name in the backups folder")
    restore_command.add_argument("--yes", action="store_true", help="don't ask first")
    worker_command = commands.add_parser(
        "ai-worker", help="do CLIP and face detection for another machine's Lumiverb (an AI machine in its Settings › AI)")
    _env_option(worker_command, argparse.SUPPRESS)
    worker_command.add_argument("--host", default="0.0.0.0", help="where it listens (every interface by default)")
    worker_command.add_argument("--port", type=int, default=DEFAULT_AI_WORKER_PORT, help="its port (6366)")
    worker_command.add_argument("--at-once", type=int, default=2, help="requests it works on at once (2)")
    worker_command.add_argument("--show-key", action="store_true", help="print its key (made if there's none) and stop")
    worker_command.add_argument("--install", action="store_true",
                                help="run it as this Mac's service (a launchd agent: at login, after upgrades)")
    worker_command.add_argument("--uninstall", action="store_true", help="stop the service and take it away")
    plugins_cli.add_parser(commands, _env_option)
    for verb, does in (("pause", "pause processing here (what's running finishes)"),
                       ("resume", "resume processing here")):
        command = commands.add_parser(
            verb, help=f"{does}: all, scans, upkeep or a producer, in every account (no API key: on this machine)")
        _env_option(command, argparse.SUPPRESS)
        command.add_argument("name", help="all, scans, upkeep or a producer by what it makes (e.g."
                                          " image.description, or a plug-in's work by its id)")
        command.add_argument("--library", help="with scans: this library's only (its name or id)")
        command.add_argument("--account", help="this account's only (its name or id)")
    args = parser.parse_args(argv)
    try:
        return {"init": _init, "migrate": _migrate, "run": _run, "setup": _setup,
                "reset-password": _reset_password, "server-admins": _server_admins, "backup": _backup,
                "restore": _restore, "ai-worker": _ai_worker, "plugins": _plugins,
                "pause": _pausing, "resume": _pausing}[args.command](args)
    except (InstallError, EnvFileError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


def entry() -> None:
    sys.exit(main())
