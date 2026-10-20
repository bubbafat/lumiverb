"""Each account's plug-ins (ADR-021, ADR-023): its switch for each one,
every acknowledgment its admins accepted, and what a plug-in has here when
it's uninstalled (holds) or deleted for good (forget).

A plug-in is on when its row says enabled and, if it declares an
acknowledgment, the version accepted when it was turned on still stands:
any version, unless the plug-in says a newer one must be accepted again
(its reaccept_from; Robert 2026-10-18). Enabled with one accepted before
that is "waiting": off until an admin accepts the new text. No row: its
default, as installed: on or off by default, with the acknowledgment the
server admin accepted for the accounts that haven't chosen
(src/server/plugin_catalog.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.plugins import version_key
from src.plugins.contract import Acknowledgment, PluginSpec

ON, OFF, WAITING = "on", "off", "waiting"


def stands(ack: Acknowledgment, accepted: str | None) -> bool:
    """Whether accepting this version (None: none accepted) still covers the acknowledgment now."""
    key = version_key(accepted)
    return key is not None and (not ack.reaccept_from or key >= version_key(ack.reaccept_from))


def state_of(spec: PluginSpec, row: dict[str, Any] | None, default: dict[str, Any] | None = None) -> str:
    """on, off, or waiting (enabled, but its acknowledgment must be accepted again since). default: its
    default as installed, for an account without a row (it hasn't chosen)."""
    if row is None:
        row = default
    if row is None or not row["enabled"]:
        return OFF
    if spec.acknowledgment is not None and not stands(spec.acknowledgment, row["acknowledged_version"]):
        return WAITING
    return ON


def on_sql(spec: PluginSpec, default: dict[str, Any] | None = None) -> str:
    """SQL true while the plug-in is on in the account: enabled, and the version of its acknowledgment
    accepted still stands (what state_of says), versions compared as numbers. spec: its manifest's, and
    default its default for an account without a row (it hasn't chosen). The id and versions are checked
    where they come from (the manifest)."""
    ack, accepted = spec.acknowledgment, ""
    if ack is not None:
        since = ",".join(str(n) for n in version_key(ack.reaccept_from)) if ack.reaccept_from else ""
        # Only a version's numbers are cast (CASE evaluates in order): never one that isn't a version.
        accepted = (" AND CASE WHEN pg.acknowledged_version ~ '^[0-9]{1,6}(\\.[0-9]{1,6}){0,3}$' THEN "
                    + (f"string_to_array(pg.acknowledged_version, '.')::int[] >= ARRAY[{since}]" if since
                       else "true") + " ELSE false END")
    chosen = f"EXISTS (SELECT 1 FROM plugins pg WHERE pg.plugin_id = '{spec.plugin_id}' AND pg.enabled{accepted})"
    if default is None:
        return chosen
    follows = "true" if state_of(spec, None, default) == ON else "false"
    return (f"(CASE WHEN EXISTS (SELECT 1 FROM plugins pg WHERE pg.plugin_id = '{spec.plugin_id}') THEN {chosen}"
            f" ELSE {follows} END)")


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
        from src.server.plugin_catalog import find

        entry = find(plugin_id)
        if entry is None:
            return False
        row = self._session.execute(text(
            "SELECT plugin_id, enabled, acknowledged_version FROM plugins WHERE plugin_id = :p"
        ), {"p": plugin_id}).mappings().first()
        return state_of(entry.spec, dict(row) if row else None, entry.installed.default) == ON

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

    def holds(self, plugin_id: str) -> dict[str, Any]:
        """What a plug-in has in this account, for the server admin uninstalling it to see: {clips: those it
        made something for, settings: whether its settings were chosen here, chosen: whether this account
        turned it on or off itself (else it follows the default)}."""
        row = self._session.execute(text(
            "SELECT (SELECT count(*) FROM plugin_outputs WHERE plugin_id = :p) AS clips,"
            " EXISTS (SELECT 1 FROM plugin_settings WHERE plugin_id = :p) AS settings,"
            " EXISTS (SELECT 1 FROM plugins WHERE plugin_id = :p) AS chosen"), {"p": plugin_id}).mappings().one()
        return dict(row)

    def forget(self, plugin_id: str) -> int:
        """Everything a plug-in left in this account but who accepted its acknowledgment (a person's, kept for
        good): what it made for each clip, its work's lineage, failures, crashes, pauses and the version seen,
        its settings and its switch. How many clips it had made something for. Doesn't commit."""
        clips = self._session.execute(text("DELETE FROM plugin_outputs WHERE plugin_id = :p"),
                                      {"p": plugin_id}).rowcount
        for sql in ("DELETE FROM artifact_lineage WHERE artifact = :p",
                    "DELETE FROM producer_crashes WHERE artifact = :p",
                    "DELETE FROM producer_pauses WHERE target = :p",
                    "DELETE FROM plugin_settings WHERE plugin_id = :p",
                    "DELETE FROM plugins WHERE plugin_id = :p",
                    "DELETE FROM system_metadata WHERE key = :seen"):
            self._session.execute(text(sql), {"p": plugin_id, "seen": f"producer.{plugin_id}.version_seen"})
        return clips
