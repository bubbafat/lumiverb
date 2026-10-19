"""What each installed plug-in's work gave back for a clip (ADR-023 phase 2):
plugin_outputs, one row per clip and plug-in, its outputs by key as
Lumiverb checked them (src/plugins/manifest.py checked_outputs), kept with
the hash of the inputs they were made from (src/server/repository/clip_inputs.py)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.shared.utils import utcnow


class PluginOutputRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, asset_id: str, plugin_id: str, outputs: dict[str, Any], inputs_hash: str) -> None:
        """The clip's outputs, in place of what it had from this plug-in. Doesn't commit."""
        self._session.execute(text(
            "INSERT INTO plugin_outputs (asset_id, plugin_id, outputs, inputs_hash, made_at)"
            " VALUES (:a, :p, CAST(:o AS jsonb), :h, :now)"
            " ON CONFLICT (asset_id, plugin_id) DO UPDATE SET outputs = EXCLUDED.outputs,"
            "   inputs_hash = EXCLUDED.inputs_hash, made_at = EXCLUDED.made_at"
        ), {"a": asset_id, "p": plugin_id, "o": json.dumps(outputs), "h": inputs_hash, "now": utcnow()})

    def of(self, asset_id: str) -> dict[str, dict[str, Any]]:
        """The clip's outputs by plug-in: {plugin_id: {outputs, inputs_hash, made_at}}."""
        return {r["plugin_id"]: {"outputs": r["outputs"], "inputs_hash": r["inputs_hash"], "made_at": r["made_at"]}
                for r in self._session.execute(text(
                    "SELECT plugin_id, outputs, inputs_hash, made_at FROM plugin_outputs WHERE asset_id = :a"
                ), {"a": asset_id}).mappings().all()}
