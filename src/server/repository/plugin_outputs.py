"""What each installed plug-in's work gave back for a clip (ADR-023 phase 2):
plugin_outputs, one row per clip and plug-in, its outputs by key as
Lumiverb checked them (src/plugins/manifest.py checked_outputs), kept with
the hash of the inputs they were made from (src/server/repository/clip_inputs.py),
and the text of those its manifest says are searchable (search_text: its
words are indexed, src/server/search/text_search.py facts).

What's shown and searched is only a plug-in's that's installed and on in the
account: turned off, what it made stays and is hidden; on again, it shows.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.shared.utils import utcnow


def search_text(work: Any, outputs: dict[str, dict[str, str] | None]) -> str:
    """The text of the outputs its work says are searchable, in its manifest's order."""
    return " · ".join(fact["text"] for o in work.outputs if o.searchable and (fact := outputs.get(o.key)))


class PluginOutputRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, asset_id: str, plugin_id: str, outputs: dict[str, Any], inputs_hash: str,
             searched: str = "") -> None:
        """The clip's outputs, in place of what it had from this plug-in. Doesn't commit."""
        self._session.execute(text(
            "INSERT INTO plugin_outputs (asset_id, plugin_id, outputs, inputs_hash, made_at, search_text)"
            " VALUES (:a, :p, CAST(:o AS jsonb), :h, :now, :s)"
            " ON CONFLICT (asset_id, plugin_id) DO UPDATE SET outputs = EXCLUDED.outputs,"
            "   inputs_hash = EXCLUDED.inputs_hash, made_at = EXCLUDED.made_at, search_text = EXCLUDED.search_text"
        ), {"a": asset_id, "p": plugin_id, "o": json.dumps(outputs), "h": inputs_hash, "now": utcnow(),
            "s": searched})

    def of(self, asset_id: str) -> dict[str, dict[str, Any]]:
        """The clip's outputs by plug-in: {plugin_id: {outputs, inputs_hash, made_at}}."""
        return {r["plugin_id"]: {"outputs": r["outputs"], "inputs_hash": r["inputs_hash"], "made_at": r["made_at"]}
                for r in self._session.execute(text(
                    "SELECT plugin_id, outputs, inputs_hash, made_at FROM plugin_outputs WHERE asset_id = :a"
                ), {"a": asset_id}).mappings().all()}


def _on_with_work(session: Session) -> list[Any]:
    """The installed plug-ins with work that are on in this account, each its Installed (manifest first)."""
    from src.server.plugin_catalog import installed
    from src.server.repository.plugins import ON, PluginRepository, state_of

    rows = PluginRepository(session).rows()
    return [p for pid, p in installed().items()
            if p.manifest.work is not None and state_of(p.manifest.spec(), rows.get(pid), p.default) == ON]


def facts_of(session: Session, asset_id: str) -> list[dict[str, Any]]:
    """What the clip's detail shows of what plug-ins made for it: each plug-in on here that made it a fact,
    in title order, {plugin_id, title, credit: {text, url} | None, facts: [{key, label, text, url}]} in its
    manifest's order (an output with nothing to say is left out). Made from inputs the clip has changed since,
    it shows until the new ones are saved."""
    made = PluginOutputRepository(session).of(asset_id)
    out = []
    for plugin in sorted(_on_with_work(session), key=lambda p: (p.manifest.title.lower(), p.manifest.plugin_id)):
        m = plugin.manifest
        outputs = (made.get(m.plugin_id) or {}).get("outputs") or {}
        facts = [{"key": o.key, "label": o.label, "text": fact["text"], "url": fact.get("url")}
                 for o in m.work.outputs if isinstance(fact := outputs.get(o.key), dict) and fact.get("text")]
        if facts:
            credit = {"text": m.credit.text, "url": m.credit.url or None} if m.credit else None
            out.append({"plugin_id": m.plugin_id, "title": m.title, "credit": credit, "facts": facts})
    return out


def searched_plugins(session: Session) -> list[str]:
    """The plug-ins whose facts a search in this account reads: on here, with a searchable output."""
    return [p.manifest.plugin_id for p in _on_with_work(session) if any(o.searchable for o in p.manifest.work.outputs)]
