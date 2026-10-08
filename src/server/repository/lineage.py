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
    # OCR is kept with the description until it has a table of its own.
    "ocr": "a.media_type = 'image' AND EXISTS (SELECT 1 FROM asset_metadata am WHERE am.asset_id = a.asset_id)",
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
    "ocr": ("EXISTS (SELECT 1 FROM asset_metadata am"
            " WHERE am.asset_id = a.asset_id AND (am.data->>'has_text') IS NOT NULL)"),
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
    return ", ".join(cols)


# Each clip's lineage, looked at once for all the rules below and joined as
# `la`: per artifact the worker is handed, the file it was made from and
# whether a failure is waiting its turn. A query using due(), outstanding(),
# source_changed() or waiting() puts LINEAGE_JOIN right after
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


def counts(session: Session, artifact: str, want: dict[str, Any], library_id: str | None = None) -> dict[str, int]:
    """{applicable, current, stale, missing, failing} for one artifact kind,
    over clips in sight (in one library when given)."""
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
        f" WHERE {APPLIES[artifact]} AND (CAST(:lib AS text) IS NULL OR a.library_id = :lib)"
    ), {"artifact": artifact, "lib": library_id, "person": PERSON, "producer": want["producer"],
        "version": want["version"], "hash": want["settings_hash"]}).one()
    applicable, n_missing, stale, failing = (int(v) for v in row)
    return {"applicable": applicable, "current": applicable - n_missing - stale, "stale": stale,
            "missing": n_missing, "failing": failing}


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
