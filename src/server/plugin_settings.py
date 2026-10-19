"""A plug-in's settings (ADR-023 phase 1c), as its manifest declares them:
each account's (its admins', in the account's plugin_settings) and the
server's (the server admin's, in the control database's
plugin_server_settings). Only what was chosen is kept; a setting without a
chosen value has its default.

Every value is checked by its setting's schema (manifest.PluginSetting.check)
before it's saved or handed on: a change names each setting it changes, a
value or null (back to its default; a secret: cleared). A secret is never
shown again, only whether it's set.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.plugins.manifest import Manifest, PluginSetting

ACCOUNT, SERVER = "account", "server"
_TABLES = {ACCOUNT: "plugin_settings", SERVER: "plugin_server_settings"}


class PluginSettingsRepository:
    """One scope's chosen values: an account's (its database's session) or the server's (the control database's)."""

    def __init__(self, session: Session, scope: str) -> None:
        self._session, self._table = session, _TABLES[scope]

    def get(self, plugin_id: str) -> dict[str, Any]:
        chosen = self._session.execute(text(f"SELECT chosen FROM {self._table} WHERE plugin_id = :p"),
                                       {"p": plugin_id}).scalar()
        return dict(chosen) if isinstance(chosen, dict) else {}

    def save(self, plugin_id: str, chosen: dict[str, Any], *, by: str | None, now: datetime) -> None:
        """Doesn't commit."""
        self._session.execute(text(
            f"INSERT INTO {self._table} (plugin_id, chosen, changed_by, changed_at)"
            " VALUES (:p, CAST(:c AS jsonb), :by, :now)"
            " ON CONFLICT (plugin_id) DO UPDATE SET chosen = EXCLUDED.chosen, changed_by = EXCLUDED.changed_by,"
            "   changed_at = EXCLUDED.changed_at"
        ), {"p": plugin_id, "c": json.dumps(chosen), "by": by, "now": now})


def changed(manifest: Manifest, scope: str, before: dict[str, Any],
            sent: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The values chosen after a change, and what's wrong with it by key (empty: nothing). Settings it doesn't
    name stay as they were; one the manifest no longer has is dropped."""
    chosen = {k: v for k, v in before.items() if (s := manifest.setting(k)) is not None and s.scope == scope}
    errors: dict[str, str] = {}
    for key, value in sent.items():
        setting = manifest.setting(key)
        if setting is None:
            errors[key] = "Not one of its settings"
        elif setting.scope != scope:
            errors[key] = ("The server's: the server admin sets it" if setting.scope == SERVER
                           else "Each account's: set in the account")
        elif value is None:
            chosen.pop(key, None)
        else:
            try:
                chosen[key] = setting.check(value)
            except ValueError as exc:
                errors[key] = str(exc)
    return chosen, errors


def values(manifest: Manifest, account: dict[str, Any], server: dict[str, Any]) -> dict[str, Any]:
    """What the plug-in is handed: every setting, as chosen in its scope or its default."""
    out: dict[str, Any] = {}
    for s in manifest.settings:
        chosen = (server if s.scope == SERVER else account).get(s.key, s.default)
        try:
            out[s.key] = s.check(chosen)
        except ValueError:
            out[s.key] = s.default  # chosen under a manifest that allowed it: its default now
    return out


def shown(setting: PluginSetting, chosen: dict[str, Any], *, editable: bool) -> dict[str, Any]:
    """One setting as Settings › Plug-ins draws it: its schema, its value (never a secret's), whether it's set."""
    value = chosen.get(setting.key, setting.default)
    secret = setting.type == "secret"
    return {"key": setting.key, "type": setting.type, "label": setting.label, "help": setting.help,
            "default": None if secret else setting.default, "value": None if secret else value,
            "set": bool(value) if secret else setting.key in chosen,
            "minimum": setting.minimum, "maximum": setting.maximum, "unit": setting.unit or None,
            "pattern": setting.pattern or None,
            "max_length": setting.max_length if setting.type in ("text", "secret") else None,
            "choices": [{"value": c.value, "label": c.label} for c in setting.choices],
            "scope": setting.scope, "advanced": setting.advanced, "remakes": setting.remakes, "editable": editable}
