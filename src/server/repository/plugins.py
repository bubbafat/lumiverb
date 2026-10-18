"""Each account's plug-ins (ADR-021): its switch for each one, and every
acknowledgment its admins accepted.

A plug-in is on when its row says enabled and, if it declares an
acknowledgment, the version accepted when it was turned on is the version
now. Enabled with an older version accepted is "waiting": off until an
admin accepts the new text. No row: off.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.plugins import plugin as plugin_spec
from src.plugins.contract import PluginSpec

ON, OFF, WAITING = "on", "off", "waiting"


def state_of(spec: PluginSpec, row: dict[str, Any] | None) -> str:
    """on, off, or waiting (enabled, its acknowledgment's text changed since it was accepted)."""
    if row is None or not row["enabled"]:
        return OFF
    if spec.acknowledgment is not None and row["acknowledged_version"] != spec.acknowledgment.version:
        return WAITING
    return ON


def on_sql(plugin_id: str) -> str:
    """SQL true while the plug-in is on in the account: enabled, and the version of its acknowledgment
    accepted the one now (what state_of says). The id and version are the registry's (checked there)."""
    spec = plugin_spec(plugin_id)
    if spec is None:
        raise ValueError(f"No plug-in {plugin_id!r}")
    accepted = f" AND pg.acknowledged_version = '{spec.acknowledgment.version}'" if spec.acknowledgment else ""
    return f"EXISTS (SELECT 1 FROM plugins pg WHERE pg.plugin_id = '{spec.plugin_id}' AND pg.enabled{accepted})"


class PluginRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def rows(self) -> dict[str, dict[str, Any]]:
        """Each plug-in's row in this account, by its id."""
        return {r["plugin_id"]: dict(r) for r in self._session.execute(text(
            "SELECT plugin_id, enabled, acknowledged_version, changed_by, changed_at FROM plugins"
        )).mappings().all()}

    def accepted(self) -> dict[str, dict[str, Any]]:
        """Each plug-in's latest acknowledgment accepted: version, user_id, acknowledged_at."""
        return {r["plugin_id"]: dict(r) for r in self._session.execute(text(
            "SELECT DISTINCT ON (plugin_id) plugin_id, version, user_id, acknowledged_at"
            " FROM plugin_acknowledgments ORDER BY plugin_id, acknowledged_at DESC, id DESC"
        )).mappings().all()}

    def is_on(self, plugin_id: str) -> bool:
        spec = plugin_spec(plugin_id)
        if spec is None:
            return False
        row = self._session.execute(text(
            "SELECT plugin_id, enabled, acknowledged_version FROM plugins WHERE plugin_id = :p"
        ), {"p": plugin_id}).mappings().first()
        return state_of(spec, dict(row) if row else None) == ON

    def set(self, plugin_id: str, enabled: bool, *, acknowledged_version: str | None, by: str | None,
            now: datetime) -> None:
        """Turn it on (with the version accepted, if it asks one) or off. Doesn't commit."""
        self._session.execute(text(
            "INSERT INTO plugins (plugin_id, enabled, acknowledged_version, changed_by, changed_at)"
            " VALUES (:p, :on, :v, :by, :now)"
            " ON CONFLICT (plugin_id) DO UPDATE SET enabled = EXCLUDED.enabled,"
            "   acknowledged_version = EXCLUDED.acknowledged_version, changed_by = EXCLUDED.changed_by,"
            "   changed_at = EXCLUDED.changed_at"
        ), {"p": plugin_id, "on": enabled, "v": acknowledged_version if enabled else None, "by": by, "now": now})

    def acknowledge(self, plugin_id: str, version: str, *, user_id: str | None, now: datetime) -> None:
        """Someone accepted this version of its text: kept for good. Doesn't commit."""
        self._session.execute(text(
            "INSERT INTO plugin_acknowledgments (plugin_id, version, user_id, acknowledged_at)"
            " VALUES (:p, :v, :u, :now)"
        ), {"p": plugin_id, "v": version, "u": user_id, "now": now})
