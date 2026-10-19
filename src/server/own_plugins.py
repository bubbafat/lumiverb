"""Lumiverb's own plug-ins (ADR-023 phase 3): programs like any other
plug-in, shipped in src/own_plugins/<id>/ (what both installs carry: the
Homebrew formula's src, a Linux install's checkout) and installed on this
server by Lumiverb itself, as the last step of migrating (`lumiverb-server
migrate`, a Mac's `run` before it starts anything, a Linux update's
scripts/migrate.sh) and again by the plug-in runner as it starts.

One that isn't installed, or that's older than the one shipped, is
installed with the same checks and copy as `lumiverb-server plugins
install`, recorded as by lumiverb: what it reads and reaches is accepted
with Lumiverb itself, so no one is asked. A new one is off by default; an
update keeps the server admin's default (on by default, an acknowledgment
that must be accepted again leaves the accounts following it waiting). One
the server admin uninstalled stays uninstalled. One that can't be installed
is said, kept in plugin_install_log with why (the Admin page says it) and
tried again at the next start: nothing here stops the server. A control
database lock keeps two installs from running at once (a migrate run by
hand beside a Mac's own).

When one is first installed, it takes as its own what the install's
settings (its env file) say for it, once: the server settings its folder's
CARRIED names, each from the variable it names, unless the server admin
chose it already.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from src.plugins import version_key
from src.plugins.manifest import MANIFEST, Manifest, ManifestError, read
from src.server.repository.installed_plugins import (
    FAILED,
    INSTALL,
    UNINSTALL,
    InstalledPluginRepository,
)
from src.shared.utils import utcnow

OWN = Path(__file__).resolve().parents[1] / "own_plugins"
BY = "lumiverb"
_LOCK = 0x4C564F57  # the control database's advisory lock while one installs ("LVOW")
LOCK_WAIT_SEC = 120  # waiting longer than this for another install, this one gives up (the next start tries)
NO_DATA_DIR = "There's no DATA_DIR in the env file, so no plug-in runner to run it: set DATA_DIR, then update again"
# In an own plug-in's folder: {"server_settings": [{"key": its server setting, "from": the install's variable,
# "unless": [values that mean its default]}]}, what it takes from the install's settings when first installed.
CARRIED = "lumiverb-own.json"


def folders() -> list[Path]:
    """Each of Lumiverb's own plug-ins as shipped: its folder."""
    return sorted(p for p in OWN.iterdir() if (p / MANIFEST).is_file()) if OWN.is_dir() else []


@cache
def shipped(plugin_id: str) -> Manifest | None:
    """One of Lumiverb's own plug-ins' manifest as shipped (None: there's none of that id)."""
    for folder in folders():
        try:
            manifest = read(folder)
        except ManifestError:
            continue
        if manifest.plugin_id == plugin_id:
            return manifest
    return None


def install_own(control_url: str, data: Path | None, *, environ: Mapping[str, str] | None = None,
                say: Callable[[str], None] = print, fail: Callable[[str], None] | None = None) -> None:
    """Lumiverb's own plug-ins installed where they aren't, or updated where the one shipped is newer; data: the
    install's DATA_DIR (None: there's none), environ its settings (the env file's: what one first installed
    takes, CARRIED). Each one's install is said (say), and a failure (fail, else say): nothing raises."""
    from src.server import database

    fail = fail or say
    # The process's own time limit on a connection (the plug-in runner's: a database that never answers holds
    # up nothing), and on waiting for another install to finish.
    timeout = database.CONNECT_TIMEOUT_SEC
    engine = create_engine(control_url, connect_args={"connect_timeout": timeout} if timeout else {})
    try:
        with engine.connect() as lock:
            lock.execute(text(f"SET lock_timeout = '{LOCK_WAIT_SEC}s'"))
            lock.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LOCK})
            try:
                with Session(engine) as session:
                    for folder in folders():
                        _one(folder, session, data, environ or {}, say, fail)
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK})
    except SQLAlchemyError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        fail(f"✗ Lumiverb's own plug-ins weren't looked at: the control database didn't answer ({first}); the "
             "next start tries again")
    finally:
        engine.dispose()


def _uninstalled(log: list[dict[str, Any]]) -> bool:
    """Whether its last install or uninstall was an uninstall: the server admin's choice, which stands."""
    return next((e["action"] for e in reversed(log) if e["action"] in (INSTALL, UNINSTALL)), None) == UNINSTALL


def _one(folder: Path, session: Session, data: Path | None, environ: Mapping[str, str],
         say: Callable[[str], None], fail: Callable[[str], None]) -> None:
    from src.server.plugin_install import PluginInstallError, install, plan

    try:
        manifest = read(folder)
    except ManifestError as exc:
        _failed(session, folder.name, None, "Its manifest isn't one this Lumiverb reads: " + "; ".join(exc.problems),
                fail)
        return
    pid, title = manifest.plugin_id, manifest.title
    repo = InstalledPluginRepository(session)
    try:
        row = repo.get(pid)
        if row is None:
            log = repo.log(pid)
            if _uninstalled(log):
                say(f"{title}: uninstalled by the server admin, so not installed again "
                    f"(`lumiverb-server plugins install {folder}` puts it back)")
                return
            if data is None:
                _failed(session, pid, manifest.version, NO_DATA_DIR, fail)
                return
            install(plan(folder, session, data), on_by_default=False, acknowledged=False, data=data,
                    session=session, by=BY, now=utcnow())
            say(f"✓ {title} {manifest.version} installed, off until an account's admin turns it on")
            if not any(e["action"] == INSTALL for e in log):
                _carry_over(session, folder, manifest, environ, say)
        elif (version_key(manifest.version) or ()) > (version_key(row["version"]) or ()) and data is not None:
            p = plan(folder, session, data)
            install(p, on_by_default=row["on_by_default"], acknowledged=False, data=data, session=session, by=BY,
                    now=utcnow(), keep_accepted=True)
            more = f" (it also: {'; '.join(p.more)}, accepted with Lumiverb)" if p.more else ""
            say(f"✓ {title} updated from {row['version']} to {manifest.version}{more}")
    except (PluginInstallError, ManifestError, OSError, SQLAlchemyError) as exc:
        session.rollback()
        _failed(session, pid, manifest.version, str(exc) or type(exc).__name__, fail)


def _failed(session: Session, plugin_id: str, version: str | None, why: str, fail: Callable[[str], None]) -> None:
    """Said, and kept with why (once, while it fails the same way)."""
    fail(f"✗ {plugin_id} couldn't be installed: {why}")
    repo = InstalledPluginRepository(session)
    try:
        last = (repo.log(plugin_id) or [{}])[-1]
        if last.get("action") != FAILED or last.get("why") != why:
            repo.failed(plugin_id, version=version, why=why[:2000], by=BY, now=utcnow())
            session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        fail(f"✗ {plugin_id}'s failure couldn't be kept: {str(exc).strip().splitlines()[0]}")


def carried(folder: Path) -> list[dict[str, Any]]:
    """What an own plug-in takes from the install's settings when first installed (its folder's CARRIED; none
    without one). ValueError when it isn't what CARRIED says."""
    try:
        raw = json.loads((folder / CARRIED).read_text())
    except FileNotFoundError:
        return []
    items = raw.get("server_settings") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not all(
            isinstance(i, dict) and isinstance(i.get("key"), str) and isinstance(i.get("from"), str)
            and isinstance(i.get("unless", []), list) for i in items):
        raise ValueError(f"{CARRIED}: server_settings is a list of {{key, from, unless}}")
    return items


def _carry_over(session: Session, folder: Path, manifest: Manifest, environ: Mapping[str, str],
                say: Callable[[str], None]) -> None:
    """What the install's settings say that a newly installed one takes as its own, once: each server setting
    CARRIED names, from its variable, unless the server admin chose it already, it means the setting's default
    (unless), or the setting's own check refuses it."""
    from src.server.plugin_settings import SERVER, PluginSettingsRepository, changed

    try:
        items = carried(folder)
    except (OSError, ValueError) as exc:
        say(f"  nothing taken from the install's settings: {exc}")
        return
    settings = PluginSettingsRepository(session, SERVER)
    chosen = settings.get(manifest.plugin_id)
    for item in items:
        key, value = item["key"], (environ.get(item["from"]) or "").strip()
        if not value or chosen.get(key) or value in item.get("unless", []):
            continue
        after, errors = changed(manifest, SERVER, chosen, {key: value})
        if errors:
            continue
        chosen = after
        settings.save(manifest.plugin_id, chosen, by=BY, now=utcnow())
        session.commit()
        say(f"  its {key}: {value} ({item['from']})")


def status(session: Session) -> list[dict[str, Any]]:
    """Each of Lumiverb's own plug-ins as this server has it, for the Admin page: {plugin_id, title, shipped
    (its version), installed (the version installed, None: none), failed (why its last install failed, while
    nothing installed it since), uninstalled (by the server admin)}."""
    repo = InstalledPluginRepository(session)
    out = []
    for folder in folders():
        try:
            manifest = read(folder)
        except ManifestError as exc:
            out.append({"plugin_id": folder.name, "title": folder.name, "shipped": None, "installed": None,
                        "failed": "; ".join(exc.problems), "uninstalled": False})
            continue
        row, log = repo.get(manifest.plugin_id), repo.log(manifest.plugin_id)
        last = log[-1] if log else {}
        out.append({"plugin_id": manifest.plugin_id, "title": manifest.title, "shipped": manifest.version,
                    "installed": row["version"] if row else None,
                    "failed": last.get("why") if last.get("action") == FAILED else None,
                    "uninstalled": row is None and _uninstalled(log)})
    return out


def main() -> int:
    """`python -m src.server.own_plugins`: scripts/migrate.sh's last step (a Linux update's), with
    CONTROL_PLANE_DATABASE_URL, DATA_DIR and what the own plug-ins take (CARRIED) in the environment. Says what
    it did; a failure is said, never this command's exit (the update goes on, and the next start tries again)."""
    url = os.environ.get("CONTROL_PLANE_DATABASE_URL", "")
    if not url:
        print("✗ CONTROL_PLANE_DATABASE_URL isn't set", file=sys.stderr)
        return 1
    data = Path(os.environ["DATA_DIR"]) if os.environ.get("DATA_DIR") else None
    if data is not None and not data.is_absolute():
        data = None
    install_own(url, data, environ=os.environ, fail=lambda line: print(line, file=sys.stderr))
    return 0


if __name__ == "__main__":
    sys.exit(main())
