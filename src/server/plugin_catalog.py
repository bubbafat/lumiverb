"""Every plug-in this server has (ADR-023): Lumiverb's own, in its code
(src/plugins/, ADR-021's registry), and those the server admin installed
(the control database's installed_plugins, each with its manifest).

An installed plug-in is there for every account. In an account whose admin
hasn't turned it on or off, it's as its default says: on or off by default,
with the acknowledgment the server admin accepted for those accounts
(Robert, 2026-10-18: "allow the system admin to install them on or off by
default to make one's config easier"). Lumiverb's own have no default: off
until an account's admin turns one on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.plugins import registry
from src.plugins.contract import PluginSpec
from src.plugins.manifest import Manifest, ManifestError, parse
from src.server.database import get_control_session
from src.server.repository.installed_plugins import InstalledPluginRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Installed:
    manifest: Manifest
    on_by_default: bool
    acknowledged_version: str | None  # what the server admin accepted for the accounts following the default
    installed_at: datetime
    changed_at: datetime

    @property
    def default(self) -> dict[str, Any]:
        """An account's switch, for an account that hasn't chosen (repository/plugins.py state_of)."""
        return {"enabled": self.on_by_default, "acknowledged_version": self.acknowledged_version}


@dataclass(frozen=True)
class Entry:
    spec: PluginSpec
    installed: Installed | None = None  # None: Lumiverb's own


def installed() -> dict[str, Installed]:
    """The installed plug-ins by id. One whose manifest this Lumiverb no longer reads (a later release
    checks more) is left out, said in the log: installing it again says why."""
    with get_control_session() as session:
        rows = InstalledPluginRepository(session).all()
    out: dict[str, Installed] = {}
    for row in rows:
        try:
            manifest = parse(row["manifest"])
        except ManifestError as exc:
            logger.warning("Plug-in %s left out: its manifest isn't one this Lumiverb reads (%s)", row["plugin_id"], exc)
            continue
        out[row["plugin_id"]] = Installed(manifest, row["on_by_default"], row["acknowledged_version"],
                                          row["installed_at"], row["changed_at"])
    return out


class OnSomewhere:
    """Of the installed plug-ins, those on in at least one active account: turned on there, or following a
    default that's on (the plug-in runner runs only these). Every account is read each time it's asked (the
    runner asks every LIST_EVERY_SEC). One whose database can't be read now keeps what it said last (nothing,
    if it never said), said once in the log until it can be read again; the others count as they are. Raises
    only when the control database can't be read: whoever asks keeps what it had."""

    def __init__(self) -> None:
        self._last: dict[str, set[str]] = {}  # each account's plug-ins on, as it last said
        self._unread: set[str] = set()  # the accounts said in the log as unreadable

    def __call__(self, found: dict[str, Installed]) -> set[str]:
        from src.server.database import get_tenant_session
        from src.server.repository.control_plane import TenantRepository
        from src.server.repository.plugins import ON, PluginRepository, state_of

        with get_control_session() as session:
            accounts = [t.tenant_id for t in TenantRepository(session).list_all() if t.status == "active"]
        on: set[str] = set()
        for tenant_id in accounts:
            try:
                with get_tenant_session(tenant_id) as session:
                    rows = PluginRepository(session).rows()
            except Exception as exc:  # noqa: BLE001 — that account's database away: what it said last stands
                if tenant_id not in self._unread:
                    self._unread.add(tenant_id)
                    logger.warning("plug-ins: account %s can't be read (%s); what it had on stands until it can",
                                   tenant_id, exc)
                on |= self._last.get(tenant_id, set()) & set(found)
                continue
            if tenant_id in self._unread:
                self._unread.discard(tenant_id)
                logger.info("plug-ins: account %s can be read again", tenant_id)
            mine = {pid for pid, p in found.items() if state_of(p.manifest.spec(), rows.get(pid), p.default) == ON}
            self._last[tenant_id] = mine
            on |= mine
        return on


def server_settings(plugin_id: str, manifest: Manifest) -> dict[str, Any]:
    """Its server-scope settings as it's handed them at its start: each as the server admin chose it, or its
    default (plugin_settings.server_values)."""
    from src.server.plugin_settings import SERVER, PluginSettingsRepository, server_values

    with get_control_session() as session:
        return server_values(manifest, PluginSettingsRepository(session, SERVER).get(plugin_id))


def entries() -> list[Entry]:
    """Every plug-in: Lumiverb's own first (their order), then the installed ones by title."""
    own = [Entry(spec) for spec in registry().values()]
    theirs = sorted((Entry(i.manifest.spec(), i) for pid, i in installed().items() if pid not in registry()),
                    key=lambda e: (e.spec.title.lower(), e.spec.plugin_id))
    return [*own, *theirs]


def find(plugin_id: str) -> Entry | None:
    if plugin_id in registry():
        return Entry(registry()[plugin_id])
    found = installed().get(plugin_id)
    return Entry(found.manifest.spec(), found) if found else None
