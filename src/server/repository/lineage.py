"""Lineage and the reconciler: how each clip's artifacts were made, and what
each clip still needs (ADR-016 phase 3).

An artifact is missing when it doesn't exist (MADE), whatever its lineage
says. One that exists is current when its lineage names the producer
registered for it now (src/shared/producers.py), at its version, with the
hash of the settings in force, made from the clip's file as it is now;
otherwise it's stale, and with no lineage at all it's an unknown producer's,
so stale too. A person's artifact (a transcript they wrote) is current
whatever the producer's settings: it's never regenerated over.

What the worker is handed (`due`): what's missing, and what was made from
a file whose content has since changed. Stale from a producer or settings
change waits for approval. A failure waits its turn: 5 minutes, doubling
up to a day.

An approval is an upgrade (piece 5): the clips stale in its scope when an
admin said yes, each handed out (`upgrade_due`) until it's made again
after the approval, and only once nothing is missing for that step in the
library (`work_conditions`). A change to the producer's settings before it
finishes drops the rest (`retire_outdated`): the new change is asked about
on its own.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.shared.producers import (
    CLIP_MODEL_ID,
    MISSING_FLAGS,
    PERSON,
    PRODUCERS,
    UNKNOWN,
    effective_settings,
    settings_hash,
)
from src.shared.utils import utcnow

# Which clips each producer applies to (SQL on active_assets a).
APPLIES: dict[str, str] = {
    "probe": "a.media_type = 'video'",
    "proxy": "true",
    "video_preview": "a.media_type = 'video'",
    "analysis_proxy": "a.media_type = 'video'",
    # Need the probe's duration first.
    "scenes": "a.media_type = 'video' AND a.duration_sec IS NOT NULL",
    "transcript": "a.media_type = 'video' AND a.duration_sec IS NOT NULL",
    "scene_vision": ("a.media_type = 'video' AND a.video_indexed"
                     " AND EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id)"),
    "vision": "a.media_type = 'image'",
    "ocr": "a.media_type = 'image'",
    "clip": "a.media_type = 'image'",
    "faces": "a.media_type = 'image'",
}
# Whether a clip has the artifact (SQL on active_assets a). One made in
# parts exists once every part does.
MADE: dict[str, str] = {
    "probe": "EXISTS (SELECT 1 FROM video_facets vf WHERE vf.asset_id = a.asset_id)",
    "proxy": "a.proxy_key IS NOT NULL",
    "video_preview": "a.video_preview_key IS NOT NULL",
    "analysis_proxy": "a.analysis_proxy_key IS NOT NULL",
    "scenes": "a.video_indexed",
    "scene_vision": ("NOT EXISTS (SELECT 1 FROM video_scenes s"
                     " WHERE s.asset_id = a.asset_id AND s.description IS NULL)"),
    "vision": "EXISTS (SELECT 1 FROM asset_metadata am WHERE am.asset_id = a.asset_id)",
    "ocr": "EXISTS (SELECT 1 FROM asset_ocr o WHERE o.asset_id = a.asset_id)",
    "clip": f"EXISTS (SELECT 1 FROM asset_embeddings ae WHERE ae.asset_id = a.asset_id AND ae.model_id = '{CLIP_MODEL_ID}')",
    "faces": "a.face_count IS NOT NULL",
    "transcript": "a.has_transcript IS NOT NULL",
}

_OVERRIDES = "producer.{}"

# Failures are tried again after 5 minutes, doubling up to a day.
RETRY_FIRST = timedelta(minutes=5)
RETRY_MAX = timedelta(days=1)


def _meta(session: Session, key: str) -> str | None:
    row = session.execute(text("SELECT value FROM system_metadata WHERE key = :k"), {"k": key}).first()
    return row[0] if row else None


def account_settings(session: Session, tenant_vision_model: str | None = None) -> dict[str, Any]:
    """Account-wide values producers take: the vision model chosen in
    Settings → AI (the tenant's, in the control plane)."""
    return {"model": tenant_vision_model or ""}


def overrides(session: Session, artifact: str) -> dict[str, Any]:
    raw = _meta(session, _OVERRIDES.format(artifact))
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def desired(session: Session, artifact: str, tenant_vision_model: str | None = None) -> dict[str, Any]:
    """What a current artifact of this kind is made with now: producer,
    version, settings and their hash."""
    p = PRODUCERS[artifact]
    settings = effective_settings(artifact, overrides(session, artifact), account_settings(session, tenant_vision_model))
    return {"producer": p.producer, "version": p.version, "settings": settings,
            "settings_hash": settings_hash(settings)}


def _stale_sql() -> str:
    return (
        "l.producer <> :person AND (l.producer <> :producer OR l.producer_version <> :version"
        " OR l.settings_hash <> :hash OR (a.sha256 IS NOT NULL AND l.source_sha256 IS DISTINCT FROM a.sha256))"
    )


def _lineage_cols() -> str:
    cols = []
    for artifact in MISSING_FLAGS.values():
        cols.append(f"max(l.source_sha256) FILTER (WHERE l.artifact = '{artifact}'"
                    f" AND l.producer NOT IN ('', '{PERSON}')) AS src_{artifact}")
        cols.append(f"bool_or(l.artifact = '{artifact}' AND l.retry_at > now()) AS wait_{artifact}")
        # When it was last made (a failed try leaves this alone).
        cols.append(f"max(l.produced_at) FILTER (WHERE l.artifact = '{artifact}'"
                    f" AND l.producer <> '' AND l.outcome <> 'failed') AS made_{artifact}")
    return ", ".join(cols)


# Each clip's lineage, looked at once for all the rules below and joined as
# `la`: per artifact the worker is handed, the file it was made from,
# whether a failure is waiting its turn and when it was last made. A query
# using due(), outstanding(), source_changed(), waiting() or upgrade_due() puts LINEAGE_JOIN right after
# `FROM active_assets a`. (One index lookup per clip: the repair summary of
# 100k clips takes about 1.4 s, against 0.75 s before lineage; a grouped
# pass over the table was slower.)
LINEAGE_JOIN = (f"LEFT JOIN LATERAL (SELECT {_lineage_cols()} FROM artifact_lineage l"
                " WHERE l.asset_id = a.asset_id) la ON TRUE")


# Ingest removes these when a file is replaced (routers/ingest.py), so they
# come back as missing; handing them out as "changed" too could loop, since
# the worker can't redo them in place.
RESET_AT_INGEST = frozenset({"analysis_proxy", "scenes", "scene_vision"})


def source_changed(artifact: str) -> str:
    """Made from a file whose content has since changed. A person's never
    counts, nor one made before the file's SHA-256 was known (unknown isn't
    changed: it's stale, waiting for approval like any other)."""
    if artifact in RESET_AT_INGEST:
        return "false"
    return f"(a.sha256 IS NOT NULL AND la.src_{artifact} IS NOT NULL AND la.src_{artifact} <> a.sha256)"


def waiting(artifact: str) -> str:
    """The last try failed and its turn hasn't come."""
    return f"COALESCE(la.wait_{artifact}, false)"


def outstanding(artifact: str) -> str:
    """Clips that need this artifact made: missing, or made from content that's changed."""
    return f"(({APPLIES[artifact]}) AND (NOT ({MADE[artifact]}) OR {source_changed(artifact)}))"


def due(artifact: str) -> str:
    """What the worker is handed now: outstanding, less failures waiting their turn."""
    return f"({outstanding(artifact)} AND NOT {waiting(artifact)})"


def _scope_sql() -> str:
    """Clips in one library and/or among some ids, when given (:lib, :ids)."""
    return ("(CAST(:lib AS text) IS NULL OR a.library_id = :lib)"
            " AND (CAST(:ids AS text[]) IS NULL OR a.asset_id = ANY(CAST(:ids AS text[])))")


def _stale_params(artifact: str, want: dict[str, Any], library_id: str | None,
                  asset_ids: list[str] | None) -> dict[str, Any]:
    return {"artifact": artifact, "lib": library_id, "ids": asset_ids, "person": PERSON,
            "producer": want["producer"], "version": want["version"], "hash": want["settings_hash"]}


def counts(session: Session, artifact: str, want: dict[str, Any], library_id: str | None = None,
           asset_ids: list[str] | None = None) -> dict[str, int]:
    """{applicable, current, stale, missing, failing} for one artifact kind,
    over clips in sight (in one library, or among asset_ids, when given)."""
    if artifact not in MADE:
        raise KeyError(artifact)
    made = f"({MADE[artifact]})"
    row = session.execute(text(
        "SELECT count(*),"
        f" count(*) FILTER (WHERE NOT {made}),"
        f" count(*) FILTER (WHERE {made} AND (l.asset_id IS NULL OR {_stale_sql()})),"
        " count(*) FILTER (WHERE l.error IS NOT NULL)"
        " FROM active_assets a"
        " LEFT JOIN artifact_lineage l ON l.asset_id = a.asset_id AND l.artifact = :artifact"
        f" WHERE {APPLIES[artifact]} AND {_scope_sql()}"
    ), _stale_params(artifact, want, library_id, asset_ids)).one()
    applicable, n_missing, stale, failing = (int(v) for v in row)
    return {"applicable": applicable, "current": applicable - n_missing - stale, "stale": stale,
            "missing": n_missing, "failing": failing}


# ---------------------------------------------------------------------------
# Upgrades: stale artifacts an admin approved making again (piece 5)
# ---------------------------------------------------------------------------

# Producers whose artifact the worker can't make again in place yet, and why.
CANT_UPGRADE: dict[str, str] = {
    "scenes": "Finding a video's scenes again means deleting the ones it has first; that isn't built yet.",
    "proxy": "Proxies and thumbnails are made when a file is scanned: lumiverb scan --force makes them again.",
    "video_preview": "Video previews are made when a file is scanned: lumiverb scan --force makes them again.",
}

# A person's edits on top of a producer's output (asset_corrections), by artifact.
EDITED: dict[str, str] = {
    "vision": ("EXISTS (SELECT 1 FROM asset_corrections c WHERE c.asset_id = a.asset_id"
               " AND (c.description IS NOT NULL OR c.tags_added <> '[]'::jsonb OR c.tags_removed <> '[]'::jsonb))"),
    "ocr": "EXISTS (SELECT 1 FROM asset_corrections c WHERE c.asset_id = a.asset_id AND c.ocr_text IS NOT NULL)",
}
# The corrections each artifact's edits are, as they're kept in correction_history.
EDIT_FIELDS: dict[str, tuple[str, ...]] = {"vision": ("description", "tags"), "ocr": ("ocr_text",)}


def stale_ids(session: Session, artifact: str, want: dict[str, Any], library_id: str | None = None,
              asset_ids: list[str] | None = None) -> tuple[list[str], set[str]]:
    """(the clips whose artifact is stale, in scope; those of them a person edited)."""
    made = f"({MADE[artifact]})"
    edited = EDITED.get(artifact, "false")
    rows = session.execute(text(
        f"SELECT a.asset_id, {edited} FROM active_assets a"
        " LEFT JOIN artifact_lineage l ON l.asset_id = a.asset_id AND l.artifact = :artifact"
        f" WHERE {APPLIES[artifact]} AND {made} AND (l.asset_id IS NULL OR {_stale_sql()}) AND {_scope_sql()}"
        " ORDER BY a.asset_id"
    ), _stale_params(artifact, want, library_id, asset_ids)).all()
    return [r[0] for r in rows], {r[0] for r in rows if r[1]}


def upgrading(artifact: str) -> str:
    """In an upgrade, and not made again since it was approved."""
    return (f"(({APPLIES[artifact]}) AND ({MADE[artifact]}) AND EXISTS (SELECT 1 FROM producer_upgrade_items ui"
            " JOIN producer_upgrades u ON u.upgrade_id = ui.upgrade_id"
            f" WHERE ui.asset_id = a.asset_id AND u.artifact = '{artifact}'"
            f" AND (la.made_{artifact} IS NULL OR la.made_{artifact} < u.approved_at)))")


def upgrade_due(artifact: str) -> str:
    """What an upgrade hands the worker now: its clips not made again yet, less failures waiting their turn."""
    return f"({upgrading(artifact)} AND NOT {waiting(artifact)})"


def upgrading_artifacts(session: Session) -> set[str]:
    return {r[0] for r in session.execute(text("SELECT DISTINCT artifact FROM producer_upgrades"))}


def retire_outdated(session: Session, tenant_vision_model: str | None) -> None:
    """Drop upgrades to settings that aren't the producer's any more: what's
    left of them would be made with newer settings nobody was asked about."""
    rows = session.execute(text(
        "SELECT upgrade_id, artifact, producer, producer_version, settings_hash FROM producer_upgrades")).all()
    if not rows:
        return
    want: dict[str, dict[str, Any]] = {}
    gone = []
    for upgrade_id, artifact, producer, version, h in rows:
        if artifact not in PRODUCERS:
            gone.append(upgrade_id)
            continue
        w = want.setdefault(artifact, desired(session, artifact, tenant_vision_model))
        if (w["producer"], w["version"], w["settings_hash"]) != (producer, version, h):
            gone.append(upgrade_id)
    if gone:
        session.execute(text("DELETE FROM producer_upgrades WHERE upgrade_id = ANY(:ids)"), {"ids": gone})
        session.commit()


def work_conditions(session: Session, flags: list[str], library_id: str,
                    tenant_vision_model: str | None) -> dict[str, str]:
    """The page filter for each flag a step asks for: what's missing first;
    an upgrade's clips once nothing is missing for it in the library."""
    from src.server.repository.tenant import MISSING_CONDITIONS

    retire_outdated(session, tenant_vision_model)
    live = upgrading_artifacts(session)
    out = {}
    for flag in flags:
        artifact = MISSING_FLAGS.get(flag)
        if artifact not in live:
            out[flag] = MISSING_CONDITIONS[flag]
            continue
        any_missing = session.execute(text(
            f"SELECT 1 FROM active_assets a {LINEAGE_JOIN}"
            f" WHERE a.library_id = :lib AND {MISSING_CONDITIONS[flag]} LIMIT 1"), {"lib": library_id}).first()
        out[flag] = MISSING_CONDITIONS[flag] if any_missing else upgrade_due(artifact)
    return out


def upgrades(session: Session, artifact: str) -> list[dict[str, Any]]:
    """The artifact's upgrades with how many clips each has left; finished ones are dropped."""
    rows = session.execute(text(
        "SELECT u.upgrade_id, u.scope, u.library_id, u.project_id, u.edits, u.approved_by, u.approved_at,"
        " COALESCE(lib.name, p.name),"
        " (SELECT count(*) FROM producer_upgrade_items ui WHERE ui.upgrade_id = u.upgrade_id)"
        " FROM producer_upgrades u"
        " LEFT JOIN libraries lib ON lib.library_id = u.library_id"
        " LEFT JOIN projects p ON p.project_id = u.project_id"
        " WHERE u.artifact = :artifact ORDER BY u.approved_at, u.upgrade_id"
    ), {"artifact": artifact}).all()
    out, finished = [], []
    for upgrade_id, scope, library_id, project_id, edits, by, at, name, total in rows:
        remaining = int(session.execute(text(
            f"SELECT count(*) FROM producer_upgrade_items ui JOIN active_assets a ON a.asset_id = ui.asset_id"
            f" {LINEAGE_JOIN}"
            f" WHERE ui.upgrade_id = :u AND ({APPLIES[artifact]}) AND ({MADE[artifact]})"
            f" AND (la.made_{artifact} IS NULL OR la.made_{artifact} < :at)"
        ), {"u": upgrade_id, "at": at}).scalar() or 0)
        if remaining == 0:
            finished.append(upgrade_id)
            continue
        out.append({"upgrade_id": upgrade_id, "scope": {"kind": scope, "id": library_id or project_id, "name": name},
                    "edits": edits, "approved_by": by, "approved_at": at, "total": int(total),
                    "remaining": remaining})
    if finished:
        session.execute(text("DELETE FROM producer_upgrades WHERE upgrade_id = ANY(:ids)"), {"ids": finished})
        session.commit()
    return out


def replace_edits(session: Session, artifact: str, asset_ids: list[str], *, by: str | None) -> int:
    """'Replace my edits': the artifact's corrections on these clips move to
    correction_history and stop showing. Returns how many clips had some."""
    from ulid import ULID

    fields = EDIT_FIELDS.get(artifact, ())
    if not asset_ids or not fields:
        return 0
    rows = session.execute(text(
        "SELECT asset_id, description, ocr_text, tags_added, tags_removed, updated_by, updated_at"
        " FROM asset_corrections WHERE asset_id = ANY(:ids) FOR UPDATE"), {"ids": asset_ids}).all()
    now = utcnow()
    touched = []
    for asset_id, description, ocr_text, added, removed, edited_by, edited_at in rows:
        kept = []
        if "description" in fields and description is not None:
            kept.append(("description", description))
        if "tags" in fields and (added or removed):
            kept.append(("tags", {"added": added or [], "removed": removed or []}))
        if "ocr_text" in fields and ocr_text is not None:
            kept.append(("ocr_text", ocr_text))
        for field, value in kept:
            session.execute(text(
                "INSERT INTO correction_history (history_id, asset_id, field, value, edited_by, edited_at, reason,"
                " replaced_by, replaced_at) VALUES (:h, :a, :f, CAST(:v AS jsonb), :eb, :ea, :why, :by, :now)"
            ), {"h": f"ch_{ULID()}", "a": asset_id, "f": field, "v": json.dumps(value), "eb": edited_by,
                "ea": edited_at, "why": f"upgrade:{artifact}", "by": by, "now": now})
        if kept:
            touched.append(asset_id)
    if touched:
        if artifact == "vision":
            clear = "description = NULL, tags_added = '[]'::jsonb, tags_removed = '[]'::jsonb"
        else:
            clear = "ocr_text = NULL"
        session.execute(text(f"UPDATE asset_corrections SET {clear}, updated_by = :by, updated_at = :now"
                             " WHERE asset_id = ANY(:ids)"), {"ids": touched, "by": by, "now": now})
        session.execute(text(
            "DELETE FROM asset_corrections WHERE asset_id = ANY(:ids) AND description IS NULL AND ocr_text IS NULL"
            " AND tags_added = '[]'::jsonb AND tags_removed = '[]'::jsonb"), {"ids": touched})
        # Search shows what's shown: sync these again.
        session.execute(text("UPDATE assets SET search_synced_at = NULL WHERE asset_id = ANY(:ids)"),
                        {"ids": touched})
    return len(touched)


def approve(session: Session, artifact: str, want: dict[str, Any], *, scope: str, library_id: str | None,
            project_id: str | None, edits: str, asset_ids: list[str], by: str | None) -> str:
    """Record an upgrade of these clips (replacing one for the same scope). Doesn't commit."""
    from ulid import ULID

    session.execute(text(
        "DELETE FROM producer_upgrades WHERE artifact = :artifact AND scope = :scope"
        " AND library_id IS NOT DISTINCT FROM :lib AND project_id IS NOT DISTINCT FROM :proj"
    ), {"artifact": artifact, "scope": scope, "lib": library_id, "proj": project_id})
    upgrade_id = f"upg_{ULID()}"
    session.execute(text(
        "INSERT INTO producer_upgrades (upgrade_id, artifact, producer, producer_version, settings_hash, scope,"
        " library_id, project_id, edits, approved_by, approved_at)"
        " VALUES (:u, :artifact, :producer, :version, :hash, :scope, :lib, :proj, :edits, :by, :now)"
    ), {"u": upgrade_id, "artifact": artifact, "producer": want["producer"], "version": want["version"],
        "hash": want["settings_hash"], "scope": scope, "lib": library_id, "proj": project_id, "edits": edits,
        "by": by, "now": utcnow()})
    session.execute(text(
        "INSERT INTO producer_upgrade_items (upgrade_id, asset_id) SELECT :u, unnest(CAST(:ids AS text[]))"
    ), {"u": upgrade_id, "ids": asset_ids})
    return upgrade_id


def record(session: Session, asset_id: str, artifact: str, lineage: dict[str, Any] | None,
           *, outcome: str = "ok", source_sha256: str | None = None, person: bool = False,
           commit: bool = True) -> None:
    """Record how an artifact was just made. A write that doesn't say (an
    old client, the macOS app today) is an unknown producer's: stale, so
    the brain makes it again. person: a person made it (only the server
    says so; a client can't). The source defaults to the clip's file now."""
    lineage = lineage or {}
    producer = PERSON if person else str(lineage.get("producer") or UNKNOWN)
    if not person and producer != PRODUCERS[artifact].producer:
        # Another kind's producer can't make this one, and a client can't
        # claim a person made it: whatever it is, it isn't current.
        producer = UNKNOWN
    source = lineage.get("source_sha256") or source_sha256
    session.execute(text(
        "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
        " source_sha256, produced_at, outcome, error, attempts, retry_at)"
        " VALUES (:a, :artifact, :producer, :version, :hash,"
        "         COALESCE(:source, (SELECT sha256 FROM assets WHERE asset_id = :a)), :now, :outcome, NULL, 0, NULL)"
        " ON CONFLICT (asset_id, artifact) DO UPDATE SET producer = EXCLUDED.producer,"
        "   producer_version = EXCLUDED.producer_version, settings_hash = EXCLUDED.settings_hash,"
        "   source_sha256 = EXCLUDED.source_sha256, produced_at = EXCLUDED.produced_at,"
        "   outcome = EXCLUDED.outcome, error = NULL, attempts = 0, retry_at = NULL"
    ), {"a": asset_id, "artifact": artifact, "producer": producer,
        "version": "" if producer in (UNKNOWN, PERSON) else str(lineage.get("version") or ""),
        "hash": "" if producer in (UNKNOWN, PERSON) else str(lineage.get("settings_hash") or ""),
        "source": source, "now": utcnow(), "outcome": outcome})
    if commit:
        session.commit()


def forget(session: Session, asset_ids: list[str], artifact: str, *, commit: bool = True) -> None:
    """The artifact is gone (a transcript deleted, scenes reset): missing again."""
    if asset_ids:
        session.execute(text("DELETE FROM artifact_lineage WHERE asset_id = ANY(:ids) AND artifact = :artifact"),
                        {"ids": asset_ids, "artifact": artifact})
        if commit:
            session.commit()


def record_failure(session: Session, asset_id: str, artifact: str, error: str, *, commit: bool = True) -> None:
    """A try that failed: kept with its error, and tried again after 5 minutes,
    doubling up to a day. An artifact made earlier stays what it was."""
    now = utcnow()
    row = session.execute(text(
        "SELECT attempts FROM artifact_lineage WHERE asset_id = :a AND artifact = :artifact FOR UPDATE"
    ), {"a": asset_id, "artifact": artifact}).first()
    attempts = (int(row[0]) if row else 0) + 1
    wait = min(RETRY_FIRST * (2 ** (attempts - 1)), RETRY_MAX)
    session.execute(text(
        "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
        " source_sha256, produced_at, outcome, error, attempts, retry_at)"
        " VALUES (:a, :artifact, '', '', '', NULL, :now, 'failed', :error, :attempts, :retry)"
        " ON CONFLICT (asset_id, artifact) DO UPDATE SET error = EXCLUDED.error,"
        "   attempts = EXCLUDED.attempts, retry_at = EXCLUDED.retry_at"
    ), {"a": asset_id, "artifact": artifact, "now": now, "error": error[:2000], "attempts": attempts,
        "retry": now + wait})
    if commit:
        session.commit()
