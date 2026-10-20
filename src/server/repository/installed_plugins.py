"""The plug-ins installed on this server (ADR-023), in the control database:
each one's version and manifest as installed, on or off by default, the
acknowledgment the server admin accepted for the accounts that follow the
default, and what it reads and reaches as accepted (installed_plugins);
every install, change of default and uninstall, kept for good, and an
install of one of Lumiverb's own that failed, with why
(plugin_install_log). One uninstalled with what it made kept, or in the
trash, is in removed_plugins (its row as installed): nothing that runs
plug-ins reads it, and installing it again takes it out."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

INSTALL, DEFAULT, UNINSTALL, FAILED, PURGE = "install", "default", "uninstall", "failed", "purge"
# What uninstalling does with what a plug-in made (Robert 2026-10-20): kept as it was, in the trash, or deleted.
KEEP, TRASH, DELETE = "keep", "trash", "delete"
_COLUMNS = ("plugin_id, version, manifest, on_by_default, acknowledged_version, permissions, installed_by,"
            " installed_at, changed_by, changed_at")
_REMOVED = ("plugin_id, version, manifest, on_by_default, acknowledged_version, permissions, kept, removed_by,"
            " removed_at, purge_after")


class InstalledPluginRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._session.execute(text(
            f"SELECT {_COLUMNS} FROM installed_plugins ORDER BY plugin_id")).mappings().all()]

    def get(self, plugin_id: str) -> dict[str, Any] | None:
        row = self._session.execute(text(f"SELECT {_COLUMNS} FROM installed_plugins WHERE plugin_id = :p"),
                                    {"p": plugin_id}).mappings().first()
        return dict(row) if row else None

    def save(self, plugin_id: str, *, version: str, manifest: dict[str, Any], on_by_default: bool,
             acknowledged_version: str | None, permissions: dict[str, Any], by: str, now: datetime) -> None:
        """Installed, or installed again (a newer version, or the same): kept with who and when. Doesn't commit."""
        self._session.execute(text(
            "INSERT INTO installed_plugins (plugin_id, version, manifest, on_by_default, acknowledged_version,"
            "   permissions, installed_by, installed_at, changed_by, changed_at)"
            " VALUES (:p, :v, CAST(:m AS jsonb), :on, :ack, CAST(:perm AS jsonb), :by, :now, :by, :now)"
            " ON CONFLICT (plugin_id) DO UPDATE SET version = EXCLUDED.version, manifest = EXCLUDED.manifest,"
            "   on_by_default = EXCLUDED.on_by_default, acknowledged_version = EXCLUDED.acknowledged_version,"
            "   permissions = EXCLUDED.permissions, changed_by = EXCLUDED.changed_by,"
            "   changed_at = EXCLUDED.changed_at"
        ), {"p": plugin_id, "v": version, "m": json.dumps(manifest), "on": on_by_default, "ack": acknowledged_version,
            "perm": json.dumps(permissions), "by": by, "now": now})
        self._log(plugin_id, INSTALL, version=version, on_by_default=on_by_default,
                  acknowledged_version=acknowledged_version, permissions=permissions, by=by, now=now)

    def set_default(self, plugin_id: str, on: bool, *, acknowledged_version: str | None, by: str,
                    now: datetime) -> None:
        """On or off by default, with the acknowledgment accepted for it (None when off). Doesn't commit."""
        self._session.execute(text(
            "UPDATE installed_plugins SET on_by_default = :on, acknowledged_version = :ack, changed_by = :by,"
            " changed_at = :now WHERE plugin_id = :p"
        ), {"p": plugin_id, "on": on, "ack": acknowledged_version, "by": by, "now": now})
        self._log(plugin_id, DEFAULT, on_by_default=on, acknowledged_version=acknowledged_version, by=by, now=now)

    def remove(self, plugin_id: str, kept: str, *, by: str, now: datetime, purge_after: datetime | None = None) -> None:
        """Uninstalled, what it made kept (KEEP), in the trash until purge_after (TRASH: its row kept in
        removed_plugins), or deleted (DELETE). Doesn't commit."""
        if kept in (KEEP, TRASH):
            self._session.execute(text(
                "INSERT INTO removed_plugins (plugin_id, version, manifest, on_by_default, acknowledged_version,"
                "   permissions, kept, removed_by, removed_at, purge_after)"
                " SELECT plugin_id, version, manifest, on_by_default, acknowledged_version, permissions, :kept, :by,"
                "   :now, :purge FROM installed_plugins WHERE plugin_id = :p"
                " ON CONFLICT (plugin_id) DO UPDATE SET version = EXCLUDED.version, manifest = EXCLUDED.manifest,"
                "   on_by_default = EXCLUDED.on_by_default, acknowledged_version = EXCLUDED.acknowledged_version,"
                "   permissions = EXCLUDED.permissions, kept = EXCLUDED.kept, removed_by = EXCLUDED.removed_by,"
                "   removed_at = EXCLUDED.removed_at, purge_after = EXCLUDED.purge_after"
            ), {"p": plugin_id, "kept": kept, "by": by, "now": now, "purge": purge_after if kept == TRASH else None})
        else:
            self._session.execute(text("DELETE FROM removed_plugins WHERE plugin_id = :p"), {"p": plugin_id})
        version = self._session.execute(text("DELETE FROM installed_plugins WHERE plugin_id = :p RETURNING version"),
                                        {"p": plugin_id}).scalar()
        self._log(plugin_id, UNINSTALL, version=version, why=kept, by=by, now=now)

    def removed(self) -> list[dict[str, Any]]:
        """The plug-ins uninstalled with what they made kept or in the trash."""
        return [dict(r) for r in self._session.execute(text(
            f"SELECT {_REMOVED} FROM removed_plugins ORDER BY plugin_id")).mappings().all()]

    def removed_one(self, plugin_id: str) -> dict[str, Any] | None:
        row = self._session.execute(text(f"SELECT {_REMOVED} FROM removed_plugins WHERE plugin_id = :p"),
                                    {"p": plugin_id}).mappings().first()
        return dict(row) if row else None

    def restored(self, plugin_id: str) -> None:
        """Installed again: no longer removed (what it made is its again). Doesn't commit."""
        self._session.execute(text("DELETE FROM removed_plugins WHERE plugin_id = :p"), {"p": plugin_id})

    def trash_due(self, now: datetime) -> list[str]:
        """The trashed plug-ins whose time is up."""
        return list(self._session.execute(text(
            "SELECT plugin_id FROM removed_plugins WHERE kept = 'trash' AND purge_after <= :now ORDER BY plugin_id"
        ), {"now": now}).scalars())

    def purged(self, plugin_id: str, *, by: str, now: datetime) -> None:
        """A removed plug-in deleted for good (the trash's time up, or what it made deleted). Doesn't commit."""
        version = self._session.execute(text("DELETE FROM removed_plugins WHERE plugin_id = :p RETURNING version"),
                                        {"p": plugin_id}).scalar()
        self._log(plugin_id, PURGE, version=version, by=by, now=now)

    def failed(self, plugin_id: str, *, version: str | None, why: str, by: str, now: datetime) -> None:
        """An install that failed (one of Lumiverb's own), and why. Doesn't commit."""
        self._log(plugin_id, FAILED, version=version, why=why, by=by, now=now)

    def log(self, plugin_id: str) -> list[dict[str, Any]]:
        """Its history, oldest first."""
        return [dict(r) for r in self._session.execute(text(
            "SELECT action, version, on_by_default, acknowledged_version, permissions, why, by, at"
            " FROM plugin_install_log WHERE plugin_id = :p ORDER BY id"), {"p": plugin_id}).mappings().all()]

    def _log(self, plugin_id: str, action: str, *, by: str, now: datetime, version: str | None = None,
             on_by_default: bool | None = None, acknowledged_version: str | None = None,
             permissions: dict[str, Any] | None = None, why: str | None = None) -> None:
        self._session.execute(text(
            "INSERT INTO plugin_install_log (plugin_id, action, version, on_by_default, acknowledged_version,"
            "   permissions, why, by, at)"
            " VALUES (:p, :a, :v, :on, :ack, CAST(:perm AS jsonb), :why, :by, :now)"
        ), {"p": plugin_id, "a": action, "v": version, "on": on_by_default, "ack": acknowledged_version,
            "perm": json.dumps(permissions) if permissions is not None else None, "why": why, "by": by, "now": now})
