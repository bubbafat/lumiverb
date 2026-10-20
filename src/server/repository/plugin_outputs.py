"""What each installed plug-in's work gave back for a clip (ADR-023 phase 2):
plugin_outputs, one row per clip and plug-in, its outputs by key as
Lumiverb checked them (src/plugins/manifest.py checked_outputs), kept with
the hash of the inputs they were made from (src/server/repository/clip_inputs.py),
and the text of those its manifest says are searchable (search_text: its
words are indexed, src/server/search/text_search.py facts). A newer version
of the plug-in that searches other outputs has its saved ones' text written
again by the search sweep (searched_again), from what they say, and one that
renames an output (its `was`, ADR-024 phase 2) has what's saved under the old
key renamed first (renamed_again): nothing is made again for either.

What's shown and searched is only a plug-in's that's installed and on in the
account, for a clip that's one of its media and still has every input its
work reads (shown_sql): turned off, or a clip whose location a person
cleared, what it made stays and is hidden; on again, or the input back, it
shows.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.server.repository.clip_inputs import present_sql
from src.shared.utils import utcnow


def search_text_sql(work: Any, outputs: str) -> str:
    """SQL for the text of the outputs its work says are searchable, from `outputs` (SQL for its outputs by key
    as saved, jsonb): each fact's text in its manifest's order, " · " between; '' for none. The keys are its
    manifest's, checked names."""
    texts = [f"({outputs})->'{o.key}'->>'text'" for o in work.outputs if o.searchable]
    return f"concat_ws(' · ', {', '.join(texts)})" if texts else "''"


def shown_sql(work: Any) -> str:
    """SQL on active_assets a: what its work made for the clip is shown and searched: the clip is one of its
    media and has every input it reads (the media and inputs are its manifest's, checked names)."""
    media = ", ".join(f"'{m}'" for m in work.media)
    return f"(a.media_type IN ({media}) AND ({present_sql(work.inputs)}))"


class PluginOutputRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, asset_id: str, plugin_id: str, work: Any, outputs: dict[str, Any], inputs_hash: str) -> None:
        """The clip's outputs from its work, in place of what it had from this plug-in, with the text of those
        it searches (search_text_sql). Doesn't commit."""
        self._session.execute(text(
            "INSERT INTO plugin_outputs (asset_id, plugin_id, outputs, inputs_hash, made_at, search_text)"
            f" VALUES (:a, :p, CAST(:o AS jsonb), :h, :now, {search_text_sql(work, 'CAST(:o AS jsonb)')})"
            " ON CONFLICT (asset_id, plugin_id) DO UPDATE SET outputs = EXCLUDED.outputs,"
            "   inputs_hash = EXCLUDED.inputs_hash, made_at = EXCLUDED.made_at, search_text = EXCLUDED.search_text"
        ), {"a": asset_id, "p": plugin_id, "o": json.dumps(outputs), "h": inputs_hash, "now": utcnow()})

    def renamed_again(self, plugin_id: str, work: Any) -> int:
        """Its saved outputs under a key a newer version renamed (an output's `was`), under their key now, as
        they were (an empty one too): how many rows. Nothing is made again: the inputs' hash and when it was
        made stay. Doesn't commit."""
        n = 0
        for o in work.outputs:
            if o.was:
                n += self._session.execute(text(
                    "UPDATE plugin_outputs SET outputs = (outputs - :was) || jsonb_build_object(:key, outputs -> :was)"
                    " WHERE plugin_id = :p AND outputs -> :was IS NOT NULL AND outputs -> :key IS NULL"
                ), {"p": plugin_id, "was": o.was, "key": o.key}).rowcount
        return n

    def searched_again(self, plugin_id: str, work: Any) -> int:
        """Its saved outputs' search text, written again where its work now searches others (a newer version
        of the plug-in): how many. Doesn't commit."""
        again = search_text_sql(work, "outputs")
        return self._session.execute(text(
            f"UPDATE plugin_outputs SET search_text = {again}"
            f" WHERE plugin_id = :p AND search_text IS DISTINCT FROM {again}"
        ), {"p": plugin_id}).rowcount

    def of(self, asset_id: str) -> dict[str, dict[str, Any]]:
        """The clip's outputs by plug-in: {plugin_id: {outputs, inputs_hash, made_at}}."""
        return {r["plugin_id"]: {"outputs": r["outputs"], "inputs_hash": r["inputs_hash"], "made_at": r["made_at"]}
                for r in self._session.execute(text(
                    "SELECT plugin_id, outputs, inputs_hash, made_at FROM plugin_outputs WHERE asset_id = :a"
                ), {"a": asset_id}).mappings().all()}

    def shown(self, asset_id: str, work: Any) -> bool:
        """Whether what its work made for this clip in sight is shown now (shown_sql)."""
        return self._session.execute(text(
            f"SELECT 1 FROM active_assets a WHERE a.asset_id = :a AND {shown_sql(work)}"
        ), {"a": asset_id}).first() is not None


def _on_with_work(session: Session) -> list[Any]:
    """The installed plug-ins with work that are on in this account, each its Installed (manifest first)."""
    from src.server.plugin_catalog import installed
    from src.server.repository.plugins import ON, PluginRepository, state_of

    rows = PluginRepository(session).rows()
    return [p for pid, p in installed().items()
            if p.manifest.work is not None and state_of(p.manifest.spec(), rows.get(pid), p.default) == ON]


def _fact(output: Any, outputs: dict[str, Any]) -> dict[str, Any] | None:
    """One output as the clip's detail shows it: its fact, or its empty line when it had nothing to say (and
    declares one); None to leave it out (nothing to say, or not asked for: an output added since)."""
    fact = outputs.get(output.key)
    if isinstance(fact, dict) and fact.get("text"):
        return {"key": output.key, "label": output.label, "text": fact["text"], "url": fact.get("url"),
                "empty": False}
    if output.empty and output.key in outputs and fact is None:
        return {"key": output.key, "label": output.label, "text": output.empty, "url": None, "empty": True}
    return None


def facts_of(session: Session, asset_id: str) -> list[dict[str, Any]]:
    """What the clip's detail shows of what plug-ins made for it: each plug-in on here that made it a fact,
    in title order, {plugin_id, title, credit: {text, url, license} | None, facts: [{key, label, text, url,
    empty}]} in its manifest's order (an output with nothing to say is its empty line, or left out). Made
    from inputs the clip has changed since, it shows until the new ones are saved; for a clip that's lost an
    input (its location cleared), it isn't shown."""
    repo = PluginOutputRepository(session)
    made = repo.of(asset_id)
    out = []
    for plugin in sorted(_on_with_work(session), key=lambda p: (p.manifest.title.lower(), p.manifest.plugin_id)):
        m = plugin.manifest
        outputs = (made.get(m.plugin_id) or {}).get("outputs") or {}
        facts = [fact for o in m.work.outputs if (fact := _fact(o, outputs)) is not None]
        if facts and repo.shown(asset_id, m.work):
            out.append({"plugin_id": m.plugin_id, "title": m.title,
                        "credit": m.credit.shown() if m.credit else None, "facts": facts})
    return out


def searched_plugins(session: Session) -> dict[str, str]:
    """The plug-ins whose facts a search in this account reads (on here, with a searchable output), each with
    the SQL on active_assets a for the clips whose facts are shown (shown_sql)."""
    return {p.manifest.plugin_id: shown_sql(p.manifest.work) for p in _on_with_work(session)
            if any(o.searchable for o in p.manifest.work.outputs)}
