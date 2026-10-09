"""Lineage and the reconciler: how each clip's artifacts were made, and what
each clip still needs (ADR-016 phase 3).

An artifact is missing when it doesn't exist (MADE), whatever its lineage
says. One that exists is current when its lineage names the producer
declared for it now (src/producers/<artifact>/), at its version, with the
hash of the settings in force, made from the clip's file as it is now;
otherwise it's stale, and with no lineage at all it's an unknown producer's,
so stale too. A person's artifact (a transcript they wrote) is current
whatever the producer's settings: it's never regenerated over.

What the scheduler is handed (`due`): what's missing, and what was made from
a file whose content has since changed. A failure waits its turn: 5 minutes,
doubling up to a day.

Stale from a producer or settings change is redone too, after anything
missing (`redo_due`, the scheduler's tier 4): changing the setting was the
approval (Robert, Oct 9). An admin can stop a producer's redo and resume it
(`pause`, `resume`); a new setting for it resumes it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.shared.producers import (
    MISSING_FLAGS,
    PERSON,
    PRODUCERS,
    UNKNOWN,
    effective_settings,
    settings_hash,
)
from src.shared.utils import utcnow

# Which clips each producer applies to, and whether a clip has its artifact
# (SQL on active_assets a; one made in parts exists once every part does).
# Each producer declares both (src/producers/<artifact>/).
APPLIES: dict[str, str] = {a: p.applies for a, p in PRODUCERS.items()}
MADE: dict[str, str] = {a: p.made for a, p in PRODUCERS.items()}

_OVERRIDES = "producer.{}"

# Failures are tried again after 5 minutes, doubling up to a day...
RETRY_FIRST = timedelta(minutes=5)
RETRY_MAX = timedelta(days=1)
# ...and given up after this many tries (about two days), until someone
# asks for it to be tried again (Robert, Oct 9).
GIVE_UP_AFTER = 10


def _meta(session: Session, key: str) -> str | None:
    row = session.execute(text("SELECT value FROM system_metadata WHERE key = :k"), {"k": key}).first()
    return row[0] if row else None


def account_settings(session: Session, job_models: Mapping[str, str] | None = None) -> dict[str, str]:
    """Account-wide values producers take: each AI job's model, chosen in
    Settings → AI (the tenant's, in the control plane)."""
    return {job: model for job, model in (job_models or {}).items() if model}


def overrides(session: Session, artifact: str) -> dict[str, Any]:
    raw = _meta(session, _OVERRIDES.format(artifact))
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def set_overrides(session: Session, artifact: str, values: Mapping[str, Any]) -> None:
    """Store the settings an admin changed (only those that differ from the
    producer's defaults). Doesn't commit."""
    key = _OVERRIDES.format(artifact)
    if values:
        session.execute(text(
            "INSERT INTO system_metadata (key, value, updated_at) VALUES (:k, :v, now())"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
            {"k": key, "v": json.dumps(dict(values), sort_keys=True)})
    else:
        session.execute(text("DELETE FROM system_metadata WHERE key = :k"), {"k": key})


def desired(session: Session, artifact: str, job_models: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What a current artifact of this kind is made with now: producer,
    version, settings and their hash. job_models: the account's model per AI job."""
    p = PRODUCERS[artifact]
    settings = effective_settings(artifact, overrides(session, artifact), account_settings(session, job_models))
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
# `la`: per artifact the scheduler is handed, the file it was made from and
# whether a failure is waiting its turn. A query using due(), outstanding(),
# source_changed(), waiting() or redo_due() puts LINEAGE_JOIN right after
# `FROM active_assets a`. (One index lookup per clip: the repair summary of
# 100k clips takes about 1.4 s, against 0.75 s before lineage; a grouped
# pass over the table was slower.)
LINEAGE_JOIN = (f"LEFT JOIN LATERAL (SELECT {_lineage_cols()} FROM artifact_lineage l"
                " WHERE l.asset_id = a.asset_id) la ON TRUE")


# Not handed out again when their clip's file changes (each producer's
# redo_on_source_change). A different file at a path is a new clip, so a
# file seldom changes under one.
NOT_REDONE_ON_SOURCE_CHANGE = frozenset(a for a, p in PRODUCERS.items() if not p.redo_on_source_change)


def source_changed(artifact: str) -> str:
    """Made from a file whose content has since changed. A person's never
    counts, nor one made before the file's SHA-256 was known (unknown isn't
    changed: it's stale, waiting for approval like any other)."""
    if artifact in NOT_REDONE_ON_SOURCE_CHANGE:
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
    """{applicable, current, stale, missing, failing, given_up} for one
    artifact kind, over clips in sight (in one library, or among asset_ids,
    when given). given_up: failing ones no longer tried (after GIVE_UP_AFTER tries)."""
    if artifact not in MADE:
        raise KeyError(artifact)
    made = f"({MADE[artifact]})"
    row = session.execute(text(
        "SELECT count(*),"
        f" count(*) FILTER (WHERE NOT {made}),"
        f" count(*) FILTER (WHERE {made} AND (l.asset_id IS NULL OR {_stale_sql()})),"
        " count(*) FILTER (WHERE l.error IS NOT NULL),"
        " count(*) FILTER (WHERE l.error IS NOT NULL AND l.retry_at = 'infinity')"
        " FROM active_assets a"
        " LEFT JOIN artifact_lineage l ON l.asset_id = a.asset_id AND l.artifact = :artifact"
        f" WHERE {APPLIES[artifact]} AND {_scope_sql()}"
    ), _stale_params(artifact, want, library_id, asset_ids)).one()
    applicable, n_missing, stale, failing, given_up = (int(v) for v in row)
    return {"applicable": applicable, "current": applicable - n_missing - stale, "stale": stale,
            "missing": n_missing, "failing": failing, "given_up": given_up}


# ---------------------------------------------------------------------------
# Redo on change: a settings change is the approval (Robert, Oct 9)
# ---------------------------------------------------------------------------

# Producers whose artifact can't be made again in place yet, and why: they
# show as stale and wait.
CANT_REDO: dict[str, str] = {a: p.cant_redo for a, p in PRODUCERS.items() if p.cant_redo}
# What each AI job's model makes (Settings → AI): a new model makes these stale.
JOB_ARTIFACTS: dict[str, tuple[str, ...]] = {
    job: tuple(a for a, p in PRODUCERS.items() if p.job == job) for job in {p.job for p in PRODUCERS.values() if p.job}
}


def redoable(artifact: str) -> bool:
    return artifact in MISSING_FLAGS.values() and artifact not in CANT_REDO


def redo_due(artifact: str) -> str:
    """Made, but with another producer, version or settings than now (:p_<a>,
    :v_<a>, :h_<a>), from the file as it is: what a redo makes again. A
    person's is never redone; one missing, or made from a file that has since
    changed, is due as missing instead; a failure waits its turn. On
    active_assets a, after LINEAGE_JOIN."""
    assert redoable(artifact), artifact
    return (f"(({APPLIES[artifact]}) AND ({MADE[artifact]}) AND NOT {source_changed(artifact)}"
            f" AND NOT {waiting(artifact)}"
            f" AND NOT EXISTS (SELECT 1 FROM artifact_lineage l WHERE l.asset_id = a.asset_id"
            f"   AND l.artifact = '{artifact}' AND (l.producer = :person"
            f"     OR (l.producer = :p_{artifact} AND l.producer_version = :v_{artifact}"
            f"         AND l.settings_hash = :h_{artifact}))))")


def redo_params(artifact: str, want: dict[str, Any]) -> dict[str, Any]:
    return {"person": PERSON, f"p_{artifact}": want["producer"], f"v_{artifact}": want["version"],
            f"h_{artifact}": want["settings_hash"]}


def paused(session: Session) -> dict[str, dict[str, Any]]:
    """Producers whose redo an admin stopped: {artifact: {paused_by, paused_at}}."""
    return {r[0]: {"paused_by": r[1], "paused_at": r[2]} for r in session.execute(text(
        "SELECT artifact, paused_by, paused_at FROM producer_redo_paused"))}


def pause(session: Session, artifact: str, *, by: str | None) -> None:
    """Stop redoing the producer's stale clips (what's missing goes on). Doesn't commit."""
    session.execute(text(
        "INSERT INTO producer_redo_paused (artifact, paused_by, paused_at) VALUES (:a, :by, :now)"
        " ON CONFLICT (artifact) DO NOTHING"), {"a": artifact, "by": by, "now": utcnow()})


def resume(session: Session, artifacts: list[str] | tuple[str, ...]) -> None:
    """Redo the producers' stale clips again. Doesn't commit."""
    if artifacts:
        session.execute(text("DELETE FROM producer_redo_paused WHERE artifact = ANY(:a)"), {"a": list(artifacts)})


def made_by_a_producer(session: Session, artifacts: list[str] | tuple[str, ...]) -> dict[str, int]:
    """Clips in sight whose artifact a producer (not a person) made, per artifact:
    what a new model for them would redo."""
    out = {}
    for artifact in artifacts:
        out[artifact] = int(session.execute(text(
            "SELECT count(*) FROM active_assets a"
            f" WHERE ({APPLIES[artifact]}) AND ({MADE[artifact]})"
            "   AND NOT EXISTS (SELECT 1 FROM artifact_lineage l WHERE l.asset_id = a.asset_id"
            "     AND l.artifact = :artifact AND l.producer = :person)"
        ), {"artifact": artifact, "person": PERSON}).scalar() or 0)
    return out


def record(session: Session, asset_id: str, artifact: str, lineage: dict[str, Any] | None,
           *, outcome: str = "ok", source_sha256: str | None = None, person: bool = False,
           produced_at: datetime | None = None, commit: bool = True) -> None:
    """Record how an artifact was just made. A write that doesn't say (an
    old client, the macOS app today) is an unknown producer's: stale, so
    the brain makes it again. person: a person made it (only the server
    says so; a client can't). The source defaults to the clip's file now.
    produced_at: when it was made, for one kept and shown again (now otherwise)."""
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
        "source": source, "now": produced_at or utcnow(), "outcome": outcome})
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
    doubling up to a day; given up after GIVE_UP_AFTER tries (retry_at
    'infinity') until someone asks for it to be tried again. An artifact
    made earlier stays what it was."""
    now = utcnow()
    row = session.execute(text(
        "SELECT attempts FROM artifact_lineage WHERE asset_id = :a AND artifact = :artifact FOR UPDATE"
    ), {"a": asset_id, "artifact": artifact}).first()
    attempts = (int(row[0]) if row else 0) + 1
    wait = min(RETRY_FIRST * (2 ** (attempts - 1)), RETRY_MAX)
    session.execute(text(
        "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
        " source_sha256, produced_at, outcome, error, attempts, retry_at, failed_at)"
        " VALUES (:a, :artifact, '', '', '', NULL, :now, 'failed', :error, :attempts,"
        "         CASE WHEN :give_up THEN 'infinity'::timestamptz ELSE :retry END, :now)"
        " ON CONFLICT (asset_id, artifact) DO UPDATE SET error = EXCLUDED.error,"
        "   attempts = EXCLUDED.attempts, retry_at = EXCLUDED.retry_at, failed_at = EXCLUDED.failed_at"
    ), {"a": asset_id, "artifact": artifact, "now": now, "error": error[:2000], "attempts": attempts,
        "retry": now + wait, "give_up": attempts >= GIVE_UP_AFTER})
    if commit:
        session.commit()


def failures(session: Session, *, artifact: str | None = None, library_id: str | None = None,
             asset_ids: list[str] | None = None, limit: int = 50, after: str | None = None) -> list[dict[str, Any]]:
    """Clips in sight whose last try failed, newest failure first: why, how
    many tries, and when the next one is (None when given up). after: the
    cursor the last page ended with."""
    rows = session.execute(text(
        "SELECT l.asset_id, l.artifact, l.error, l.attempts, l.failed_at, l.retry_at,"
        " a.rel_path, a.library_id, a.media_type, lib.name AS library_name"
        " FROM artifact_lineage l JOIN active_assets a ON a.asset_id = l.asset_id"
        " JOIN libraries lib ON lib.library_id = a.library_id"
        " WHERE l.error IS NOT NULL"
        "   AND (CAST(:artifact AS text) IS NULL OR l.artifact = :artifact)"
        "   AND (CAST(:lib AS text) IS NULL OR a.library_id = :lib)"
        "   AND (CAST(:ids AS text[]) IS NULL OR a.asset_id = ANY(CAST(:ids AS text[])))"
        "   AND (CAST(:after AS text) IS NULL"
        "        OR (COALESCE(l.failed_at, l.produced_at), l.asset_id || '/' || l.artifact)"
        "           < (CAST(split_part(:after, '|', 1) AS timestamptz), split_part(:after, '|', 2)))"
        " ORDER BY COALESCE(l.failed_at, l.produced_at) DESC, l.asset_id || '/' || l.artifact DESC"
        " LIMIT :n"
    ), {"artifact": artifact, "lib": library_id, "ids": asset_ids, "after": after, "n": limit}).mappings().all()
    out = []
    for r in rows:
        given_up = r["retry_at"] is not None and r["retry_at"].year >= 9999
        when = r["failed_at"]
        out.append({**r, "given_up": given_up, "retry_at": None if given_up else r["retry_at"],
                    "cursor": f"{when.isoformat() if when else ''}|{r['asset_id']}/{r['artifact']}"})
    return out


def retry(session: Session, *, artifact: str | None = None, library_id: str | None = None,
          asset_ids: list[str] | None = None) -> int:
    """Try these failing clips again now, given up or not (the back-off starts
    over). Doesn't commit. Returns how many."""
    r = session.execute(text(
        "UPDATE artifact_lineage l SET retry_at = NULL, attempts = 0 FROM active_assets a"
        " WHERE a.asset_id = l.asset_id AND l.error IS NOT NULL"
        "   AND (CAST(:artifact AS text) IS NULL OR l.artifact = :artifact)"
        "   AND (CAST(:lib AS text) IS NULL OR a.library_id = :lib)"
        "   AND (CAST(:ids AS text[]) IS NULL OR l.asset_id = ANY(CAST(:ids AS text[])))"
    ), {"artifact": artifact, "lib": library_id, "ids": asset_ids})
    if r.rowcount:
        # The scheduler doesn't wait out its own hour for clips it just tried.
        session.execute(text(
            "INSERT INTO system_metadata (key, value, updated_at) VALUES ('scheduler.retry_at', :now, now())"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
            {"now": utcnow().isoformat()})
    return int(r.rowcount or 0)
