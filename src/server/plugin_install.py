"""Installing a plug-in on this server (ADR-023 phase 1a): the server
admin's, on the machine (`lumiverb-server plugins`).

Its folder is checked (its manifest, its size), copied into the install
(DATA_DIR/plugins/<id>/program/, read-only, swapped in whole so nothing
runs half a version) and recorded in the control database with what it
reads and reaches as accepted, and whether it's on or off by default in
every account that hasn't chosen (Robert, 2026-10-18). On by default, its
acknowledgment, if it has one, is accepted by the server admin for those
accounts. A newer version that reads or reaches more asks again; one that
asks the same or less doesn't. Uninstalling stops it being offered and
removes its folder.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from src.plugins.manifest import MANIFEST, Manifest, asks_more, parse, read
from src.server.repository.installed_plugins import InstalledPluginRepository
from src.server.repository.plugins import stands

# Written into each installed copy: which install it is, so the runner starts a new one afresh (a folder's
# inode isn't: a filesystem may give a new folder the number of one just removed).
INSTALL_MARK = ".lumiverb-install"
# Lumiverb's example plug-in (every kind of setting, its own check): to try Settings › Plug-ins with.
EXAMPLE = Path(__file__).resolve().parents[1] / "plugin_examples" / "example"
# A plug-in's folder, at most: more is a mistake (the wrong folder named).
MAX_BYTES = 4 * 1024**3
MAX_FILES = 100_000


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

    @property
    def asks(self) -> bool:
        """Its permissions need accepting: a first install, or an update asking more."""
        return self.before is None or bool(self.more)

    def on_by_default(self, choice: bool | None) -> bool:
        if choice is not None:
            return choice
        if self.before is None:
            raise PluginInstallError("Say --on or --off: whether it's on in every account that hasn't turned it "
                                     "on or off itself")
        return bool(self.before["on_by_default"])

    def acknowledgment_asked(self, on: bool) -> bool:
        """On by default, its acknowledgment is the server admin's to accept, unless what they accepted stands."""
        ack = self.manifest.acknowledgment
        if not on or ack is None:
            return False
        accepted = self.before.get("acknowledged_version") if self.before else None
        return not (accepted and stands(ack, accepted))


def plan(folder: Path, session: Session, data: Path) -> Plan:
    """Raises ManifestError or PluginInstallError when it can't be installed."""
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise PluginInstallError(f"No folder at {folder}")
    manifest = read(folder)
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
    before = InstalledPluginRepository(session).get(manifest.plugin_id)
    more = asks_more(before["permissions"], manifest.permissions()) if before else []
    return Plan(manifest, folder, before, more)


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
        accepted = ack.version if p.acknowledgment_asked(True) else p.before["acknowledged_version"]
    home = plugin_dir(data, p.manifest.plugin_id)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    copy = home / f".program-{uuid.uuid4().hex}"
    try:
        shutil.copytree(p.folder, copy, symlinks=True)
        (copy / INSTALL_MARK).write_text(uuid.uuid4().hex)
        _read_only(copy)
        InstalledPluginRepository(session).save(
            p.manifest.plugin_id, version=p.manifest.version, manifest=_as_json(p.folder),
            on_by_default=on_by_default, acknowledged_version=accepted, permissions=p.manifest.permissions(),
            by=by, now=now)
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


def uninstall(plugin_id: str, *, data: Path, session: Session, by: str, now: datetime) -> None:
    """No longer offered to any account; its folder (its program and its own data) removed."""
    repo = InstalledPluginRepository(session)
    if repo.get(plugin_id) is None:
        raise PluginInstallError(f"No plug-in {plugin_id} is installed")
    repo.remove(plugin_id, by=by, now=now)
    session.commit()
    _remove(plugin_dir(data, plugin_id))
