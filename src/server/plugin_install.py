"""Installing a plug-in on this server (ADR-023 phase 1a): the server
admin's, on the machine (`lumiverb-server plugins`).

Its folder is checked (its manifest, its size), copied into the install
(DATA_DIR/plugins/<id>/program/, read-only, swapped in whole so nothing
runs half a version) and recorded in the control database with what it
reads and reaches as accepted, and whether it's on or off by default in
every account that hasn't chosen (Robert, 2026-10-18). On by default, its
acknowledgment, if it has one, is accepted by the server admin for those
accounts. A newer version that reads or reaches more asks again; one that
asks the same or less doesn't.

Uninstalling (Robert, 2026-10-20: an informed decision) first says what it
has (impact: each account's clips it made something for, its settings, its
own data folder's size) and what stops working without it, then does one of
three with what it made:
- keep: its facts stay shown and searched in every account as before, its
  settings and its own data folder kept; installing it again picks it all
  up, nothing made again (a broken upgrade path: uninstall, install anew);
- trash: hidden, kept TRASH_DAYS, then deleted for good by upkeep
  (purge_trash); installing it again before then brings it back;
- delete: what it made, its settings and its own data, gone now from every
  account (who accepted its acknowledgment is kept: a person's record).
Its program goes whichever: it's no longer run, nor its work done.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session

from src.plugins.manifest import MANIFEST, Manifest, asks_more, parse, read
from src.producers.names import is_reserved
from src.server.repository.installed_plugins import DELETE, KEEP, TRASH, InstalledPluginRepository
from src.server.repository.plugins import stands

# Written into each installed copy: which install it is, so the runner starts a new one afresh (a folder's
# inode isn't: a filesystem may give a new folder the number of one just removed).
INSTALL_MARK = ".lumiverb-install"
# Lumiverb's example plug-in (every kind of setting, its own check): to try Settings › Plug-ins with.
EXAMPLE = Path(__file__).resolve().parents[1] / "plugin_examples" / "example"
# A plug-in's folder, at most: more is a mistake (the wrong folder named).
MAX_BYTES = 4 * 1024**3
MAX_FILES = 100_000
# Days a plug-in uninstalled to the trash keeps what it made before it's deleted for good (Robert, 2026-10-20).
TRASH_DAYS = 30


class PluginInstallError(Exception):
    pass


def plugin_dir(data: Path, plugin_id: str) -> Path:
    """Everything of one plug-in's on this server: its program, its own data, its socket."""
    return data / "plugins" / plugin_id


def program_dir(data: Path, plugin_id: str) -> Path:
    return plugin_dir(data, plugin_id) / "program"


def install_mark(program: Path) -> str | None:
    """Which install a program folder is (INSTALL_MARK), None when it says none."""
    try:
        return (program / INSTALL_MARK).read_text().strip() or None
    except OSError:
        return None


@dataclass(frozen=True)
class Plan:
    """What installing a folder would do, to show before it's done."""

    manifest: Manifest
    folder: Path
    before: dict[str, Any] | None  # its row, when a version is installed already
    more: list[str]  # what it reads or reaches that the version accepted didn't, in words
    # Uninstalled with what it made kept or in the trash (removed_plugins): installing it brings that back.
    removed: dict[str, Any] | None = None

    @property
    def asks(self) -> bool:
        """Its permissions need accepting: a first install, or an update asking more."""
        return self.before is None or bool(self.more)

    def on_by_default(self, choice: bool | None) -> bool:
        if choice is not None:
            return choice
        if self.before is None and self.removed is not None:
            return bool(self.removed["on_by_default"])  # as it was before it was uninstalled
        if self.before is None:
            raise PluginInstallError("Say --on or --off: whether it's on in every account that hasn't turned it "
                                     "on or off itself")
        return bool(self.before["on_by_default"])

    def acknowledgment_asked(self, on: bool) -> bool:
        """On by default, its acknowledgment is the server admin's to accept, unless what they accepted stands."""
        ack = self.manifest.acknowledgment
        if not on or ack is None:
            return False
        was = self.before or self.removed
        accepted = was.get("acknowledged_version") if was else None
        return not (accepted and stands(ack, accepted))


def plan(folder: Path, session: Session, data: Path) -> Plan:
    """Raises ManifestError or PluginInstallError when it can't be installed."""
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise PluginInstallError(f"No folder at {folder}")
    manifest = read(folder)
    if is_reserved(manifest.plugin_id):
        raise PluginInstallError(
            f"A plug-in's id can't be {manifest.plugin_id!r}: Lumiverb uses that word (its work, pools and names share"
            " the plug-ins' namespace, ADR-024). Give it another id in its manifest.")
    plugins = (data / "plugins").resolve()
    if data.resolve().is_relative_to(folder) or folder.is_relative_to(plugins):
        raise PluginInstallError(f"{folder} is Lumiverb's own folder, not a plug-in's")
    files = size = 0
    for root, _dirs, names in os.walk(folder):
        files += len(names)
        size += sum(os.lstat(os.path.join(root, n)).st_size for n in names)
        if files > MAX_FILES or size > MAX_BYTES:
            raise PluginInstallError(f"{folder} holds more than a plug-in does ({MAX_FILES:,} files or "
                                     f"{MAX_BYTES // 1024**3} GB): is it the right folder?")
    repo = InstalledPluginRepository(session)
    before = repo.get(manifest.plugin_id)
    more = asks_more(before["permissions"], manifest.permissions()) if before else []
    return Plan(manifest, folder, before, more, None if before else repo.removed_one(manifest.plugin_id))


def _read_only(path: Path) -> None:
    for root, _dirs, names in os.walk(path):
        for n in names:
            p = os.path.join(root, n)
            if not os.path.islink(p):
                os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) & ~0o222)
        os.chmod(root, 0o555)


def _remove(path: Path) -> None:
    """A folder, read-only ones too."""
    if not path.exists():
        return
    for root, _dirs, _ in os.walk(path):
        os.chmod(root, 0o755)
    shutil.rmtree(path)


def install(p: Plan, *, on_by_default: bool, acknowledged: bool, data: Path, session: Session, by: str,
            now: datetime, keep_accepted: bool = False) -> None:
    """Install it (or this version of it). acknowledged: the server admin accepted its acknowledgment, when it's
    on by default and asks (Plan.acknowledgment_asked). keep_accepted: no one is asked (Lumiverb's own, updated:
    src/server/own_plugins.py), so what the server admin accepted stays as it was, and the accounts following
    the default wait when it no longer covers the new text. Copied first, recorded, then swapped in."""
    ack = p.manifest.acknowledgment
    if p.acknowledgment_asked(on_by_default) and not acknowledged and not keep_accepted:
        raise PluginInstallError(f"On by default, {ack.title} is accepted for every account that hasn't chosen: "
                                 "accept it, or install it --off for each account's admin to")
    accepted = None
    if keep_accepted:
        accepted = p.before["acknowledged_version"] if p.before and on_by_default else None
    elif on_by_default and ack is not None:
        accepted = ack.version if p.acknowledgment_asked(True) else (p.before or p.removed)["acknowledged_version"]
    home = plugin_dir(data, p.manifest.plugin_id)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    copy = home / f".program-{uuid.uuid4().hex}"
    try:
        shutil.copytree(p.folder, copy, symlinks=True)
        (copy / INSTALL_MARK).write_text(uuid.uuid4().hex)
        _read_only(copy)
        repo = InstalledPluginRepository(session)
        repo.save(p.manifest.plugin_id, version=p.manifest.version, manifest=_as_json(p.folder),
                  on_by_default=on_by_default, acknowledged_version=accepted, permissions=p.manifest.permissions(),
                  by=by, now=now)
        repo.restored(p.manifest.plugin_id)  # what it made, kept or in the trash, is its again
        session.commit()
    except BaseException:
        session.rollback()
        _remove(copy)
        raise
    program, old = home / "program", home / f".program-old-{uuid.uuid4().hex}"
    if program.exists():
        program.rename(old)
    copy.rename(program)
    _remove(old)


def _as_json(folder: Path) -> dict[str, Any]:
    """The manifest as installed: its JSON as read (checked by read)."""
    return json.loads((folder / MANIFEST).read_bytes())


def set_default(plugin_id: str, on: bool, *, acknowledged: bool, session: Session, by: str, now: datetime) -> None:
    """On or off by default. On, its acknowledgment (if any) is the server admin's to accept again, as an
    account admin's turning it on asks every time (ADR-021)."""
    repo = InstalledPluginRepository(session)
    row = repo.get(plugin_id)
    if row is None:
        raise PluginInstallError(f"No plug-in {plugin_id} is installed")
    ack = parse(row["manifest"]).acknowledgment
    if on and ack is not None and not acknowledged:
        raise PluginInstallError(f"On by default, {ack.title} is accepted for every account that hasn't chosen: "
                                 "accept it first")
    repo.set_default(plugin_id, on, acknowledged_version=ack.version if on and ack else None, by=by, now=now)
    session.commit()


@dataclass(frozen=True)
class Impact:
    """What uninstalling a plug-in takes away, for the server admin to see first."""

    title: str
    accounts: list[dict[str, Any]]  # each {tenant_id, name, clips, settings, chosen} it has something in
    unreadable: list[str]  # the accounts whose databases can't be read now (what they hold isn't known)
    data_mb: int  # its own data folder
    stops: list[str]  # what stops working without it, in words


def _accounts(session: Session) -> list[Any]:
    """Every account whose database there is (a deleted one's isn't)."""
    from src.server.repository.control_plane import TenantRepository

    return [t for t in TenantRepository(session).list_all() if t.status != "deleted"]


def dependents(plugin_id: str) -> list[str]:
    """The plug-ins that read what this one makes, in words: none can yet, as a plug-in reads only Lumiverb's
    own data (protocol 1). ADR-024 phase 3 (plug-ins reading each other's) names them here, and on Settings ›
    Plug-ins, as broken chains."""
    return []


def impact(plugin_id: str, *, session: Session, data: Path) -> Impact:
    """What it has and what stops working without it. PluginInstallError when it isn't installed."""
    from src.server.database import get_tenant_session
    from src.server.repository.plugins import PluginRepository

    row = InstalledPluginRepository(session).get(plugin_id)
    if row is None:
        raise PluginInstallError(f"No plug-in {plugin_id} is installed")
    m = parse(row["manifest"])
    accounts, unreadable = [], []
    for tenant in _accounts(session):
        try:
            with get_tenant_session(tenant.tenant_id) as tenant_session:
                held = PluginRepository(tenant_session).holds(plugin_id)
        except Exception:  # noqa: BLE001 — that account's database away: said, not guessed
            unreadable.append(tenant.name)
            continue
        if held["clips"] or held["settings"] or held["chosen"]:
            accounts.append({"tenant_id": tenant.tenant_id, "name": tenant.name, **held})
    stops = []
    if m.work is not None:
        stops.append(f"{m.work.title} stops being made for new and changed clips")
    if "places" in m.lookups:
        stops.append("Set location's Place search no longer finds places from it")
    stops += dependents(plugin_id)
    return Impact(m.title, accounts, unreadable, _folder_mb(plugin_dir(data, plugin_id) / "data"), stops)


def _folder_mb(folder: Path) -> int:
    total = 0
    for root, _dirs, names in os.walk(folder):
        total += sum(os.lstat(os.path.join(root, n)).st_size for n in names)
    return total // 2**20


def uninstall(plugin_id: str, kept: str, *, data: Path, session: Session, by: str, now: datetime) -> list[str]:
    """No longer offered to any account, nor run; what it made kept (KEEP), in the trash for TRASH_DAYS
    (TRASH), or deleted now from every account with its own data (DELETE). The accounts whose databases
    couldn't be read (DELETE: what it made there stays, hidden)."""
    if kept not in (KEEP, TRASH, DELETE):
        raise PluginInstallError("Say whether what it made is kept, put in the trash, or deleted")
    repo = InstalledPluginRepository(session)
    if repo.get(plugin_id) is None:
        raise PluginInstallError(f"No plug-in {plugin_id} is installed")
    repo.remove(plugin_id, kept, by=by, now=now, purge_after=now + timedelta(days=TRASH_DAYS))
    if kept == DELETE:
        _forget_server(plugin_id, session)
    session.commit()
    home = plugin_dir(data, plugin_id)
    if kept != DELETE:  # its program goes; its own data stays for it to pick up
        for path in (home / "program", home / "tmp"):
            _remove(path)
        (home / "server-settings.json").unlink(missing_ok=True)
        return []
    unreadable = _forget_everywhere(plugin_id, session)
    _remove(home)
    return unreadable


def _forget_server(plugin_id: str, session: Session) -> None:
    from src.server.plugin_settings import SERVER, PluginSettingsRepository

    PluginSettingsRepository(session, SERVER).forget(plugin_id)


def _forget_everywhere(plugin_id: str, session: Session) -> list[str]:
    """What it left in every account deleted; the accounts that couldn't be read."""
    from src.server.database import get_tenant_session
    from src.server.repository.plugins import PluginRepository

    unreadable = []
    for tenant in _accounts(session):
        try:
            with get_tenant_session(tenant.tenant_id) as tenant_session:
                PluginRepository(tenant_session).forget(plugin_id)
                tenant_session.commit()
        except Exception:  # noqa: BLE001 — that account's database away: what it holds stays, hidden
            unreadable.append(tenant.name)
    return unreadable


def purge_trash(*, data: Path, session: Session, now: datetime) -> list[str]:
    """Upkeep: each plug-in in the trash past its TRASH_DAYS deleted for good, from every account, with its
    own data. The ids purged (one an account's database couldn't be read for waits for the next upkeep)."""
    repo = InstalledPluginRepository(session)
    purged = []
    for plugin_id in repo.trash_due(now):
        if _forget_everywhere(plugin_id, session):
            continue
        _forget_server(plugin_id, session)
        repo.purged(plugin_id, by="upkeep", now=now)
        session.commit()
        _remove(plugin_dir(data, plugin_id))
        purged.append(plugin_id)
    return purged
