"""Backups of what only Lumiverb holds: every database, the control plane's
(people who log in, accounts, keys) and each account's (libraries, clips,
names, faces, ratings, notes, projects, settings), as pg_dump makes them.
Previews, the search index and models aren't in them: they're made again
from the databases and the originals.

A backup is a folder, `lumiverb-<local time>-<kind>`, in the install's
backups folder (DATA_DIR/backups, or the one an admin chose on the machine
itself: DATA_DIR/backups.json). It holds one pg_dump archive per database
and manifest.json (when, which kind, each database's file, size and
SHA-256). It's written as `<name>.partial` and renamed when whole, so a
folder without .partial is a whole backup. Readable by this machine's user
only: they hold password hashes.

Kinds: nightly (the 5-minute upkeep starts one when none has been made
since the latest 03:00 here, so a Mac asleep then makes it on waking), now
(an admin's Back up now, or `lumiverb-server backup`), before-upgrade (a
Mac's service, before migrations it hasn't run yet) and before-restore
(made by a restore first, so it can be undone).

Kept: every backup of the last 7 days, the newest of each of the 4 latest
weeks that have one, the newest 3 before an upgrade and 3 before a
restore, and always the newest. Only folders it made are removed (a lumiverb- name and a
manifest); anything else in the folder is left alone.

Restore (`lumiverb-server restore`, with Lumiverb stopped) puts each
database back beside the live one and swaps it in only once whole, so a
failed restore changes nothing; then migrates to this version and remakes
the search index from what was restored.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import monotonic, sleep

from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import SQLAlchemyError

from src.server import drives, write_check

logger = logging.getLogger(__name__)

KINDS = ("nightly", "now", "before-upgrade", "before-restore")
SETTINGS_FILE = "backups.json"
STATE_FILE = "backups-state.json"
LOCK_FILE = "backups.lock"
MANIFEST = "manifest.json"
PARTIAL = ".partial"
PREFIX = "lumiverb-"
_NAME = re.compile(r"^lumiverb-\d{4}-\d{2}-\d{2}-\d{6}-(" + "|".join(KINDS) + r")(\.partial)?$")

NIGHTLY_AT = time(3, 0)
# A failed backup is tried again this much later, not at every upkeep.
RETRY_AFTER = timedelta(hours=1)
# running() looks at the lock by taking it for an instant (Settings › Backups
# asks every 2 s while one runs; Back up now asks as it starts one). A backup
# starting in that instant waits this long for it before taking the holder
# for another backup: a look lets go at once, a backup holds it to the end.
LOOK_WAIT_SEC = 2.0
KEEP_DAYS = timedelta(days=7)
KEEP_WEEKS = 4
KEEP_BEFORE = 3
# A .partial this old was a backup that died (the API restarted mid-way).
PARTIAL_STALE = timedelta(days=1)
# A connection that's closing (Lumiverb just stopped, or the restore's own
# check before) is still listed for a moment after its client lets go: what's
# connected is looked at again for this long before a restore is refused.
CLOSING_GRACE_SEC = 3.0
# Restoring: the database put back beside the live one, then swapped in.
RESTORING = "__restoring"
REPLACED = "__replaced"


class BackupError(Exception):
    """A backup or restore couldn't be made; the message says why, plainly."""


@dataclass(frozen=True)
class Database:
    role: str  # control | account
    url: str
    tenant_id: str | None = None

    @property
    def name(self) -> str:
        return make_url(self.url).database or ""

    @property
    def file(self) -> str:
        return f"{self.tenant_id or 'control_plane'}.dump"


@dataclass
class Backup:
    path: Path
    made_at: datetime  # UTC
    kind: str
    databases: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def bytes(self) -> int:
        return sum(int(d.get("bytes") or 0) for d in self.databases)

    @property
    def accounts(self) -> int:
        return sum(1 for d in self.databases if d.get("role") == "account")


# -- Where -----------------------------------------------------------------------


def default_folder(data_dir: Path) -> Path:
    return data_dir / "backups"


def folder(data_dir: Path) -> Path:
    """The backups folder: an admin's choice, else DATA_DIR/backups."""
    try:
        chosen = json.loads((data_dir / SETTINGS_FILE).read_text()).get("folder")
    except (OSError, ValueError, AttributeError):
        chosen = None
    return Path(chosen) if isinstance(chosen, str) and chosen else default_folder(data_dir)


def check_folder(path: str | Path) -> Path:
    """The folder, when backups can go there: a full path to a folder this
    machine's Lumiverb can write. Raises BackupError saying why not."""
    raw = str(path).strip()
    if not raw:
        raise BackupError("Say which folder.")
    chosen = Path(raw).expanduser()
    if not chosen.is_absolute():
        raise BackupError("Use a full path, such as /Volumes/Backup/Lumiverb.")
    if (why := drives.missing(Path(os.path.abspath(chosen)))) is not None:
        raise BackupError(why)
    if not chosen.is_dir():
        raise BackupError(f"There's no folder at {chosen}.")
    if (why := write_check.try_write(chosen)) is not None:
        raise BackupError(why)
    return chosen


def set_folder(data_dir: Path, path: str | Path | None) -> Path:
    """Backups go to `path` from now on (None: back to DATA_DIR/backups).
    Those already made stay where they are."""
    settings = data_dir / SETTINGS_FILE
    if path is None:
        settings.unlink(missing_ok=True)
        return default_folder(data_dir)
    chosen = check_folder(path)
    _write_json(settings, {"folder": str(chosen)})
    return chosen


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(json.dumps(value, indent=2))
    partial.replace(path)


# -- What ------------------------------------------------------------------------


def databases(control_url: str) -> list[Database]:
    """The control plane, then each account's database (from its routing)."""
    from src.server.lifecycle.migrate import MigrationError, tenant_databases

    try:
        accounts = tenant_databases(control_url)
    except MigrationError as exc:
        raise BackupError(str(exc)) from None
    return [Database("control", control_url), *(Database("account", url, tenant_id) for tenant_id, url in accounts)]


def pg_tool(name: str, pg_bin: str | None = None) -> str:
    """PostgreSQL's `name` (pg_dump, pg_restore): PG_BIN's, else the PATH's."""
    if pg_bin:
        found = Path(pg_bin) / name
        if found.is_file():
            return str(found)
        raise BackupError(f"There's no {name} in PG_BIN ({pg_bin}).")
    found = shutil.which(name)
    if not found:
        raise BackupError(f"There's no {name}: install PostgreSQL's client tools, or set PG_BIN.")
    return found


def _libpq(url: str) -> tuple[list[str], dict[str, str]]:
    """A database URL as pg_dump's and pg_restore's options, and the password
    in their environment (never on a command line others can see)."""
    target = make_url(url)
    args = []
    host = target.host or target.query.get("host")
    if isinstance(host, tuple):
        host = host[0]
    if host:
        args += ["--host", str(host)]
    if target.port:
        args += ["--port", str(target.port)]
    if target.username:
        args += ["--username", target.username]
    env = {**os.environ}
    if target.password:
        env["PGPASSWORD"] = str(target.password)
    return args, env


def _major(version_text: str) -> int | None:
    found = re.search(r"(\d+)(?:\.\d+)?", version_text)
    return int(found.group(1)) if found else None


def _check_versions(dump: str, control_url: str) -> str:
    """pg_dump's version, refused when it's older than the server's (it then
    can't read it)."""
    said = subprocess.run([dump, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    engine = _admin_engine(control_url)  # the server's own database: a new install's may have no other yet
    try:
        with engine.connect() as conn:
            server = int(conn.execute(text("SHOW server_version_num")).scalar()) // 10000
    except SQLAlchemyError as exc:
        raise BackupError(f"Can't reach the database: {_first(exc)}") from None
    finally:
        engine.dispose()
    tool = _major(said)
    if tool is not None and tool < server:
        raise BackupError(f"{dump} is PostgreSQL {tool}'s; the database is {server}: use {server}'s (PG_BIN).")
    return said


def _first(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -- Making one ------------------------------------------------------------------


def _name(made_at: datetime, kind: str) -> str:
    return f"{PREFIX}{made_at.astimezone().strftime('%Y-%m-%d-%H%M%S')}-{kind}"


def make(dest: Path, control_url: str, kind: str, *, pg_bin: str | None = None,
         progress: Callable[[int, int], None] | None = None, now: Callable[[], datetime] | None = None) -> Backup:
    """A whole backup of every database in `dest`. Raises BackupError (what
    it had written is removed)."""
    if kind not in KINDS:
        raise ValueError(f"Unknown kind of backup: {kind!r}")
    dump = pg_tool("pg_dump", pg_bin)
    version = _check_versions(dump, control_url)
    every = databases(control_url)
    made_at = (now or (lambda: datetime.now(UTC)))()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"Can't make {dest}: {exc.strerror or exc}") from None
    final = dest / _name(made_at, kind)
    work = final.with_name(final.name + PARTIAL)
    if final.exists():
        raise BackupError(f"{final} is there already.")
    shutil.rmtree(work, ignore_errors=True)
    try:
        work.mkdir(mode=0o700)
        listed = []
        for done, db in enumerate(every):
            if progress:
                progress(done, len(every))
            out = work / db.file
            args, env = _libpq(db.url)
            result = subprocess.run(
                [dump, *args, "--format=custom", "--no-password", f"--file={out}", db.name],
                env=env, capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                said = (result.stderr or result.stdout).strip().splitlines() or [f"pg_dump exited {result.returncode}"]
                raise BackupError(f"pg_dump couldn't back up {db.name}: {said[-1]}")
            out.chmod(0o600)
            listed.append({"role": db.role, "tenant_id": db.tenant_id, "database": db.name, "file": db.file,
                           "bytes": out.stat().st_size, "sha256": _sha256(out)})
        if progress:
            progress(len(every), len(every))
        _write_json(work / MANIFEST, {"made_at": made_at.astimezone(UTC).isoformat(), "kind": kind,
                                      "pg_dump": version, "databases": listed})
        (work / MANIFEST).chmod(0o600)
        work.rename(final)
    except BackupError:
        shutil.rmtree(work, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise BackupError(f"Can't write the backup in {dest}: {exc.strerror or exc}") from None
    return Backup(final, made_at.astimezone(UTC), kind, listed)


# -- The ones there ----------------------------------------------------------------


def read(path: Path) -> Backup:
    """The backup in `path`. Raises BackupError when it isn't a whole one."""
    if path.name.endswith(PARTIAL):
        raise BackupError(f"{path} was never finished.")
    try:
        manifest = json.loads((path / MANIFEST).read_text())
        made_at = datetime.fromisoformat(manifest["made_at"])
        kind = manifest["kind"]
        listed = list(manifest["databases"])
    except FileNotFoundError:
        raise BackupError(f"{path} isn't a Lumiverb backup (no {MANIFEST}).") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BackupError(f"Can't read {path / MANIFEST}: {exc}") from None
    if made_at.tzinfo is None:
        made_at = made_at.replace(tzinfo=UTC)
    return Backup(path, made_at, kind, listed)


def list_backups(where: Path) -> list[Backup]:
    """The whole backups in `where`, newest first."""
    try:
        entries = list(where.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        if not entry.is_dir() or not _NAME.match(entry.name) or entry.name.endswith(PARTIAL):
            continue
        with contextlib.suppress(BackupError):
            found.append(read(entry))
    return sorted(found, key=lambda b: b.made_at, reverse=True)


def to_keep(backups: list[Backup], now: datetime) -> set[Path]:
    """Which backups the keeping rule keeps (module docstring)."""
    keep: set[Path] = set()
    newest_first = sorted(backups, key=lambda b: b.made_at, reverse=True)
    if newest_first:
        keep.add(newest_first[0].path)
    weeks: dict[tuple[int, int], Backup] = {}
    before: dict[str, int] = {}
    for b in newest_first:
        if now - b.made_at <= KEEP_DAYS:
            keep.add(b.path)
        weeks.setdefault(tuple(b.made_at.astimezone().isocalendar())[:2], b)
        if b.kind.startswith("before-") and before.get(b.kind, 0) < KEEP_BEFORE:
            before[b.kind] = before.get(b.kind, 0) + 1
            keep.add(b.path)
    for week in sorted(weeks, reverse=True)[:KEEP_WEEKS]:
        keep.add(weeks[week].path)
    return keep


def prune(where: Path, now: datetime | None = None) -> list[Path]:
    """Removes the backups the keeping rule doesn't keep, and unfinished ones
    a day old. Returns what it removed."""
    now = now or datetime.now(UTC)
    backups = list_backups(where)
    keep = to_keep(backups, now)
    removed = [b.path for b in backups if b.path not in keep]
    try:
        for entry in where.iterdir():
            if (entry.is_dir() and _NAME.match(entry.name) and entry.name.endswith(PARTIAL)
                    and now - datetime.fromtimestamp(entry.stat().st_mtime, UTC) > PARTIAL_STALE):
                removed.append(entry)
    except OSError:
        pass
    for path in removed:
        shutil.rmtree(path, ignore_errors=True)
        logger.info("Backup removed (kept: the last 7 days, 4 weeks): %s", path.name)
    return removed


# -- When ---------------------------------------------------------------------------


def latest_nightly_time(now_local: datetime) -> datetime:
    """The latest 03:00 at or before `now_local` (local wall-clock time)."""
    today = now_local.replace(hour=NIGHTLY_AT.hour, minute=NIGHTLY_AT.minute, second=0, microsecond=0)
    return today if now_local >= today else today - timedelta(days=1)


def nightly_due(newest: datetime | None, now: datetime, last_failed_at: datetime | None = None) -> bool:
    """None made since the latest 03:00 here (whatever kind), and none failed
    within the hour. `now` and the times are aware."""
    if last_failed_at is not None and now - last_failed_at < RETRY_AFTER:
        return False
    if newest is None:
        return True
    since = latest_nightly_time(now.astimezone().replace(tzinfo=None))
    return newest.astimezone().replace(tzinfo=None) < since


def next_nightly(now: datetime) -> datetime:
    """The next 03:00 here, aware."""
    local = now.astimezone()
    latest = latest_nightly_time(local.replace(tzinfo=None))
    return (latest + timedelta(days=1)).replace(tzinfo=local.tzinfo)


def next_at(newest: datetime | None, now: datetime, last_failed_at: datetime | None = None) -> datetime:
    """When upkeep next makes the nightly one: now when it's due (none since
    03:00, or none at all), an hour after one that failed, else the next 03:00."""
    if nightly_due(newest, now):
        if last_failed_at is not None and now - last_failed_at < RETRY_AFTER:
            return last_failed_at + RETRY_AFTER
        return now
    return next_nightly(now)


# -- Running (one at a time per install) ------------------------------------------


def read_state(data_dir: Path) -> dict:
    try:
        state = json.loads((data_dir / STATE_FILE).read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _update_state(data_dir: Path, **changes) -> None:
    _write_json(data_dir / STATE_FILE, {**read_state(data_dir), **changes})


@contextlib.contextmanager
def _one_at_a_time(data_dir: Path, *, wait: float = 0.0) -> Iterator[None]:
    """Held while a backup is made: across the API's workers and the CLI.
    `wait`: how long to wait out a look (running())."""
    data_dir.mkdir(parents=True, exist_ok=True)
    deadline = monotonic() + wait
    with open(data_dir / LOCK_FILE, "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if monotonic() >= deadline:
                    raise BackupError("A backup is being made already.") from None
                sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def running(data_dir: Path) -> bool:
    """Whether a backup holds the lock now: a look, taking it for an instant."""
    try:
        with _one_at_a_time(data_dir):
            return False
    except BackupError:
        return True
    except OSError:
        return False


def back_up(data_dir: Path, control_url: str, kind: str, *, pg_bin: str | None = None,
            dest: Path | None = None) -> Backup:
    """A backup now, into the backups folder (or `dest`), then the old ones
    pruned. Records how it went for Settings › Backups and the Admin page.
    Raises BackupError."""
    with _one_at_a_time(data_dir, wait=LOOK_WAIT_SEC):
        started = datetime.now(UTC)
        _update_state(data_dir, running={"kind": kind, "started_at": started.isoformat(), "done": 0, "of": 0})

        def progress(done: int, of: int) -> None:
            _update_state(data_dir, running={"kind": kind, "started_at": started.isoformat(), "done": done, "of": of})

        try:
            backup = make(dest or folder(data_dir), control_url, kind, pg_bin=pg_bin, progress=progress)
        except Exception as exc:  # noqa: BLE001 — whatever stopped it is what Settings › Backups says
            said = str(exc) if isinstance(exc, BackupError) else f"{type(exc).__name__}: {_first(exc)}"
            _update_state(data_dir, running=None, last_error=said, last_failed_at=datetime.now(UTC).isoformat())
            logger.error("Backup failed: %s", said)
            raise exc if isinstance(exc, BackupError) else BackupError(said) from exc
        _update_state(data_dir, running=None, last_error=None, last_failed_at=None,
                      last_made_at=backup.made_at.isoformat())
        logger.info("Backed up %d database(s) to %s (%d bytes)", len(backup.databases), backup.path, backup.bytes)
        if dest is None:
            prune(folder(data_dir))
        return backup


def start_nightly_if_due(data_dir: Path, control_url: str, *, pg_bin: str | None = None,
                         now: datetime | None = None) -> str:
    """Upkeep's turn: starts the nightly backup in the background when it's
    due. Returns "started", "running" or "not due"."""
    now = now or datetime.now(UTC)
    if running(data_dir):
        return "running"
    backups = list_backups(folder(data_dir))
    state = read_state(data_dir)
    failed = parse_time(state.get("last_failed_at"))
    if not nightly_due(backups[0].made_at if backups else None, now, failed):
        return "not due"
    start(data_dir, control_url, "nightly", pg_bin=pg_bin)
    return "started"


def start(data_dir: Path, control_url: str, kind: str, *, pg_bin: str | None = None) -> threading.Thread:
    """A backup in a thread of its own (the API answers meanwhile)."""

    def work() -> None:
        with contextlib.suppress(BackupError):
            back_up(data_dir, control_url, kind, pg_bin=pg_bin)

    thread = threading.Thread(target=work, name=f"backup-{kind}", daemon=True)
    thread.start()
    return thread


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


# -- Restoring -----------------------------------------------------------------------


def verify(backup: Backup) -> None:
    """Every file the manifest lists is there, whole. Raises BackupError."""
    if not backup.databases or not any(d.get("role") == "control" for d in backup.databases):
        raise BackupError(f"{backup.path} has no control plane in it.")
    for d in backup.databases:
        path = backup.path / str(d.get("file"))
        if not path.is_file():
            raise BackupError(f"{path} is missing.")
        if _sha256(path) != d.get("sha256"):
            raise BackupError(f"{path} isn't the file that was backed up (its SHA-256 differs).")


def _admin_engine(url: str):
    """The server's own `postgres` database, outside any transaction (CREATE,
    DROP and RENAME DATABASE need that)."""
    return create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connected_elsewhere(urls: list[str], *, grace: float | None = None) -> list[str]:
    """The clients connected to these databases now, besides this one:
    Lumiverb still running (its API and scheduler name themselves:
    lumiverb-api, lumiverb-scheduler), or someone's psql. Empty when there
    are none. Only clients: PostgreSQL's own workers (autovacuum tidying a
    database) don't count. One that's closing is gone within the grace
    (CLOSING_GRACE_SEC): only those still there after it are said."""
    names = sorted({make_url(u).database for u in urls if make_url(u).database})
    if not names:
        return []
    grace = CLOSING_GRACE_SEC if grace is None else grace
    deadline = monotonic() + grace
    engine = _admin_engine(urls[0])
    try:
        while True:
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT datname, coalesce(nullif(application_name, ''), host(client_addr), 'local')"
                    " FROM pg_stat_activity WHERE datname = ANY(:names) AND pid <> pg_backend_pid()"
                    " AND backend_type = 'client backend'"), {"names": names}).all()
            if not rows or monotonic() >= deadline:
                break
            sleep(0.2)
    finally:
        engine.dispose()
    return sorted({f"{name} ({who})" for name, who in rows})


def _restore_one(url: str, archive: Path, pg_restore: str, say: Callable[[str], None]) -> None:
    """The archive's database put back as `url`'s: restored beside it, then
    swapped in. Raises BackupError with the live database untouched."""
    name = make_url(url).database
    if not name:
        raise BackupError(f"No database named in {make_url(url).render_as_string(hide_password=True)}")
    side, old = name + RESTORING, name + REPLACED
    engine = _admin_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {_quoted(side)} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {_quoted(side)}"))
        args, env = _libpq(url)
        result = subprocess.run(
            [pg_restore, *args, "--no-password", f"--dbname={side}", "--single-transaction", "--exit-on-error",
             "--no-owner", "--no-privileges", "--no-comments", str(archive)],
            env=env, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            said = (result.stderr or result.stdout).strip().splitlines() or [f"pg_restore exited {result.returncode}"]
            with engine.connect() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS {_quoted(side)} WITH (FORCE)"))
            raise BackupError(f"pg_restore couldn't put back {name}: {said[-1]}")
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar()
            conn.execute(text(f"DROP DATABASE IF EXISTS {_quoted(old)} WITH (FORCE)"))
            if exists:
                conn.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"),
                    {"n": name})
                conn.execute(text(f"ALTER DATABASE {_quoted(name)} RENAME TO {_quoted(old)}"))
            conn.execute(text(f"ALTER DATABASE {_quoted(side)} RENAME TO {_quoted(name)}"))
            if exists:
                conn.execute(text(f"DROP DATABASE {_quoted(old)} WITH (FORCE)"))
    except SQLAlchemyError as exc:
        raise BackupError(f"Can't put back {name}: {_first(exc)}") from None
    finally:
        engine.dispose()
    say(f"  ✓ {name}")


def restore(backup: Backup, control_url: str, *, tenant_url_template: str | None, pg_bin: str | None = None,
            say: Callable[[str], None] = print) -> list[str]:
    """Puts every database in the backup back, Lumiverb stopped. The accounts
    are this install's again (their routing: tenant_url_template), and each
    one's search index is remade from what was restored. Returns the
    databases that aren't in the backup, left as they were. Raises
    BackupError, before changing anything when it can."""
    verify(backup)
    pg_restore = pg_tool("pg_restore", pg_bin)
    _check_versions(pg_restore, control_url)
    try:
        now_there = databases(control_url)
    except BackupError:
        now_there = [Database("control", control_url)]  # a new install's: nothing else yet
    busy = connected_elsewhere([d.url for d in now_there])
    if busy:
        raise BackupError("Lumiverb is still running (connected: " + ", ".join(busy) + "). Stop it first.")

    def url_of(entry: dict) -> str:
        if entry.get("role") == "control":
            return control_url
        tenant_id = str(entry.get("tenant_id"))
        if tenant_url_template:
            return tenant_url_template.format(tenant_id=tenant_id)
        return next((d.url for d in now_there if d.tenant_id == tenant_id), "")

    targets = [(entry, url_of(entry)) for entry in backup.databases]
    if any(not url for _, url in targets):
        raise BackupError("TENANT_DATABASE_URL_TEMPLATE isn't set: there's nowhere to put the accounts back.")
    # The control plane last: until it's back, the install still lists what it had.
    for entry, url in sorted(targets, key=lambda t: t[0].get("role") == "control"):
        _restore_one(url, backup.path / str(entry["file"]), pg_restore, say)

    engine = create_engine(control_url)
    try:
        with engine.begin() as conn:
            for entry, url in targets:
                if entry.get("role") == "account":
                    conn.execute(text("UPDATE tenant_db_routing SET connection_string = :url WHERE tenant_id = :t"),
                                 {"url": url, "t": entry["tenant_id"]})
    except SQLAlchemyError as exc:
        raise BackupError(f"Put back, but the accounts' routing wasn't updated: {_first(exc)}") from None
    finally:
        engine.dispose()
    restored = {make_url(url).database for _, url in targets}
    return sorted(d.name for d in now_there if d.name not in restored)
