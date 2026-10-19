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
(`pause` and `resume` with scope REDO); a new setting for it resumes it. A
new producer version (a deploy, which nobody approved) stops its redo the
first time the scheduler sees clips made by an older one
(`hold_new_version`): an admin resumes it.

Pausing (Robert, Oct 9): one switch per processing action, Scans, Upkeep
and each producer the scheduler makes, each paused and resumed on its own
(scope WORK: nothing of it starts, missing or stale); a producer's redo is
its switch's other scope (REDO: its stale clips wait, what's missing is
made). Both live in producer_pauses and are read in one go (`pauses`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.producers.live import LiveMap, LiveSet, Made
from src.shared.content_hash import FP1, FULL_UP_TO, SHA256
from src.shared.producers import (
    MADE_AT_SCAN,
    MISSING_FLAGS,
    PERSON,
    PRODUCERS,
    UNKNOWN,
    effective_settings,
    level_of,
    notches,
    settings_hash,
    use_settings,
)
from src.shared.utils import utcnow


# Which clips each producer applies to, and whether a clip has its artifact
# (SQL on active_assets a; one made in parts exists once every part does).
# Each producer declares both (src/producers/<artifact>/).
def _applies(p) -> str:
    """A producer's applies, and for one a plug-in brings, only while that plug-in is on in the account."""
    if not p.plugin:
        return p.applies
    from src.server.repository.plugins import on_sql

    return f"({on_sql(p.plugin)}) AND ({p.applies})"


# Each, and the tables below, as the producers are now (src/producers/live.py).
APPLIES: Mapping[str, str] = LiveMap(lambda: {a: _applies(p) for a, p in PRODUCERS.items()})
MADE: Mapping[str, str] = LiveMap(lambda: {a: p.made for a, p in PRODUCERS.items()})
# Made, but due to be checked again (location guesses): "false" for the rest.
RECHECK: Mapping[str, str] = LiveMap(lambda: {a: p.recheck or "false" for a, p in PRODUCERS.items()})

_OVERRIDES = "producer.{}"

# Failures are tried again after 5 minutes, doubling up to a day...
RETRY_FIRST = timedelta(minutes=5)
RETRY_MAX = timedelta(days=1)
# ...and given up after this many tries (about two days), until someone
# asks for it to be tried again (Robert, Oct 9).
GIVE_UP_AFTER = 10
# A job that crashes (no clip charged: the API or its database away, a
# process that died) holds its clips back an hour, uncharged. The same clip
# crashing this many times in a row is charged a failure, so the back-off
# and giving up apply (producer_crashes; a success or a failure clears it).
CRASHES_BEFORE_CHARGE = 3


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


def uses(session: Session, artifact: str) -> dict[str, Any]:
    """The producer's settings that don't remake, as the account has them now."""
    return use_settings(artifact, overrides(session, artifact))


def desired(session: Session, artifact: str, job_models: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What a current artifact of this kind is made with now: producer,
    version, settings and their hash, and its level (None without a level
    setting: see below). job_models: the account's model per AI job."""
    p = PRODUCERS[artifact]
    changed = overrides(session, artifact)
    settings = effective_settings(artifact, changed, account_settings(session, job_models))
    return {"producer": p.producer, "version": p.version, "settings": settings,
            "settings_hash": settings_hash(settings), "level": level_of(artifact, uses(session, artifact)),
            "notches": notches(artifact, changed)}


def _stale_sql() -> str:
    return (
        "l.producer <> :person AND (l.producer <> :producer OR l.producer_version <> :version"
        " OR l.settings_hash <> :hash"
        " OR (a.content_hash IS NOT NULL AND l.source_hash IS DISTINCT FROM a.content_hash))"
    )


def _lineage_cols() -> str:
    cols = []
    for artifact in (*MISSING_FLAGS.values(), *MADE_AT_SCAN):
        cols.append(f"max(l.source_hash) FILTER (WHERE l.artifact = '{artifact}'"
                    f" AND l.producer NOT IN ('', '{PERSON}')) AS src_{artifact}")
        cols.append(f"bool_or(l.artifact = '{artifact}' AND l.retry_at > now()) AS wait_{artifact}")
    return ", ".join(cols)


# Each clip's lineage, looked at once for all the rules below and joined as
# `la`: per artifact the scheduler is handed, the file it was made from and
# whether a failure is waiting its turn. A query using due(), outstanding(),
# source_changed(), waiting() or redo_due() puts lineage_join() right after
# `FROM active_assets a`. (One index lookup per clip: the repair summary of
# 100k clips takes about 1.4 s, against 0.75 s before lineage; a grouped
# pass over the table was slower.)
lineage_join = Made(lambda: f"LEFT JOIN LATERAL (SELECT {_lineage_cols()} FROM artifact_lineage l"
                            " WHERE l.asset_id = a.asset_id) la ON TRUE").now


# Not handed out again when their clip's file changes (each producer's
# redo_on_source_change). A different file at a path is a new clip, so a
# file seldom changes under one.
NOT_REDONE_ON_SOURCE_CHANGE: Set[str] = LiveSet(
    lambda: {a for a, p in PRODUCERS.items() if not p.redo_on_source_change})


def source_changed(artifact: str) -> str:
    """Made from a file whose content has since changed. A person's never
    counts, nor one made before the file's content hash was known (unknown isn't
    changed: it's stale, waiting for approval like any other)."""
    if artifact in NOT_REDONE_ON_SOURCE_CHANGE:
        return "false"
    return (f"(a.content_hash IS NOT NULL AND la.src_{artifact} IS NOT NULL"
            f" AND la.src_{artifact} <> a.content_hash)")


def waiting(artifact: str) -> str:
    """The last try failed and its turn hasn't come."""
    return f"COALESCE(la.wait_{artifact}, false)"


def outstanding(artifact: str) -> str:
    """Clips that need this artifact made: missing, or made from content that's changed."""
    return f"(({APPLIES[artifact]}) AND (NOT ({MADE[artifact]}) OR {source_changed(artifact)}))"


def due(artifact: str) -> str:
    """Missing work now (the missing_* filters and counts): outstanding, less
    failures waiting their turn."""
    return f"({outstanding(artifact)} AND NOT {waiting(artifact)})"


def rechecking(artifact: str) -> str:
    """Made, and due to be checked again (not missing): the scheduler hands
    these out with what's missing, less failures waiting their turn."""
    return (f"(({APPLIES[artifact]}) AND ({MADE[artifact]}) AND ({RECHECK[artifact]})"
            f" AND NOT {waiting(artifact)})")


def handed_out(artifact: str) -> str:
    """What the scheduler's queue hands out for a producer's kind: due, or rechecking."""
    return f"({due(artifact)} OR {rechecking(artifact)})"


def made_first(artifact: str) -> str:
    """Its clip has what the producer is made from (its needs): "true" for one made from the original alone."""
    return " AND ".join(f"({MADE[n]})" for n in PRODUCERS[artifact].needs) or "true"


def queued(artifact: str, *, redo: bool = False) -> str:
    """The one rule for a producer's work: what the scheduler's queue lists
    (scheduler/queue.py), and for one that reads the originals what a
    library's reader is given (src/server/reader_work.py). Due or rechecking
    (handed_out), or for its redo made another way than now (redo_due, with
    redo_params); either only once what it's made from is (made_first). On
    active_assets a, after lineage_join()."""
    return f"(({redo_due(artifact) if redo else handed_out(artifact)}) AND ({made_first(artifact)}))"


_WITHIN = "(CAST(:within AS text[]) IS NULL OR a.library_id = ANY(CAST(:within AS text[])))"


def _scope_sql() -> str:
    """Clips in one library, among some ids, and in some libraries (the ones
    a person can see), when given (:lib, :ids, :within)."""
    return ("(CAST(:lib AS text) IS NULL OR a.library_id = :lib)"
            " AND (CAST(:ids AS text[]) IS NULL OR a.asset_id = ANY(CAST(:ids AS text[])))"
            f" AND {_WITHIN}")


def _stale_params(artifact: str, want: dict[str, Any], library_id: str | None,
                  asset_ids: list[str] | None, within: list[str] | None = None) -> dict[str, Any]:
    return {"artifact": artifact, "lib": library_id, "ids": asset_ids, "within": within, "person": PERSON,
            "producer": want["producer"], "version": want["version"], "hash": want["settings_hash"],
            "level": want.get("level")}


def _below_sql(artifact: str) -> str:
    """Made below the level now (:level): the producer's own, current but for
    its level (a level isn't in the hash), at a lower one or none recorded.
    A person's never is: it's current whatever the settings. "false" for a
    producer without a level setting."""
    if PRODUCERS[artifact].level is None:
        return "false"
    return (f"(l.asset_id IS NOT NULL AND l.producer <> :person AND NOT ({_stale_sql()})"
            f" AND NOT ({RECHECK[artifact]}) AND (l.level IS NULL OR l.level < :level))")


def _changed_sql(artifact: str) -> str:
    """source_changed() on the producer's own lineage row (l): made by a
    producer from a file whose content has since changed."""
    if artifact in NOT_REDONE_ON_SOURCE_CHANGE:
        return "false"
    return ("(a.content_hash IS NOT NULL AND l.producer NOT IN ('', :person) AND l.source_hash IS NOT NULL"
            " AND l.source_hash <> a.content_hash)")


# A failure isn't waiting its turn (nor given up): on the producer's own lineage row (l).
_NOT_WAITING = "(l.retry_at IS NULL OR l.retry_at <= now())"


def _due_new_sql(artifact: str) -> str:
    """What the scheduler hands out as missing, on the producer's own lineage
    row (l): not made, made from a file that's changed, or due to be checked
    again; less failures waiting their turn."""
    made = f"({MADE[artifact]})"
    return (f"({_NOT_WAITING} AND (NOT {made} OR ({made} AND ({RECHECK[artifact]}))"
            f" OR ({made} AND {_changed_sql(artifact)})))")


def _due_redo_sql(artifact: str) -> str:
    """What its redo hands out (stale, from the file as it is), on l: less
    failures waiting their turn."""
    made = f"({MADE[artifact]})"
    return (f"({_NOT_WAITING} AND {made} AND (l.asset_id IS NULL OR {_stale_sql()})"
            f" AND NOT ({RECHECK[artifact]}) AND NOT {_changed_sql(artifact)})")


def counts(session: Session, artifact: str, want: dict[str, Any], library_id: str | None = None,
           asset_ids: list[str] | None = None, within: list[str] | None = None) -> dict[str, int]:
    """{applicable, current, stale, missing, rechecking, below, failing,
    given_up} for one artifact kind, over clips in sight (in one library,
    among asset_ids, or in the libraries within, when given). rechecking: made, and due to be checked
    again (not stale). below: made below the producer's level now (its level
    setting; not stale, and not current: made again only when asked,
    redo_below). given_up: failing ones no longer tried (after GIVE_UP_AFTER tries).

    And for why its work waits (src/server/producer_waits.py): due_new, what
    the scheduler hands out as missing now; due_redo, what its redo hands out
    while it runs; retrying, failing ones waiting their turn (not given up)."""
    if artifact not in MADE:
        raise KeyError(artifact)
    made = f"({MADE[artifact]})"
    stale = f"(l.asset_id IS NULL OR {_stale_sql()})"
    row = session.execute(text(
        "SELECT count(*),"
        f" count(*) FILTER (WHERE NOT {made}),"
        f" count(*) FILTER (WHERE {made} AND {stale}),"
        f" count(*) FILTER (WHERE {made} AND NOT {stale} AND ({RECHECK[artifact]})),"
        f" count(*) FILTER (WHERE {made} AND {_below_sql(artifact)}),"
        " count(*) FILTER (WHERE l.error IS NOT NULL),"
        " count(*) FILTER (WHERE l.error IS NOT NULL AND l.retry_at = 'infinity'),"
        f" count(*) FILTER (WHERE {_due_new_sql(artifact)}),"
        f" count(*) FILTER (WHERE {_due_redo_sql(artifact)}),"
        " count(*) FILTER (WHERE l.error IS NOT NULL AND l.retry_at > now() AND l.retry_at <> 'infinity')"
        " FROM active_assets a"
        " LEFT JOIN artifact_lineage l ON l.asset_id = a.asset_id AND l.artifact = :artifact"
        f" WHERE {APPLIES[artifact]} AND {_scope_sql()}"
    ), _stale_params(artifact, want, library_id, asset_ids, within)).one()
    (applicable, n_missing, n_stale, n_rechecking, n_below, failing, given_up,
     due_new, due_redo, retrying) = (int(v) for v in row)
    return {"applicable": applicable, "current": applicable - n_missing - n_stale - n_rechecking - n_below,
            "stale": n_stale, "missing": n_missing, "rechecking": n_rechecking, "below": n_below,
            "failing": failing, "given_up": given_up, "due_new": due_new, "due_redo": due_redo,
            "retrying": retrying}


def due_by_library(session: Session, artifact: str, want: dict[str, Any], library_ids: list[str], *,
                   redo: bool, asset_ids: list[str] | None = None) -> dict[str, int]:
    """Clips due in each of these libraries, as counts' due_new (and due_redo
    when its redo runs): what waits while a library's storage can't be read."""
    if not library_ids:
        return {}
    due = f"({_due_new_sql(artifact)} OR {_due_redo_sql(artifact)})" if redo else _due_new_sql(artifact)
    rows = session.execute(text(
        "SELECT a.library_id, count(*) FROM active_assets a"
        " LEFT JOIN artifact_lineage l ON l.asset_id = a.asset_id AND l.artifact = :artifact"
        f" WHERE {APPLIES[artifact]} AND {_scope_sql()} AND a.library_id = ANY(:libs) AND {due}"
        " GROUP BY a.library_id"
    ), {**_stale_params(artifact, want, None, asset_ids), "libs": list(library_ids)}).all()
    return {r[0]: int(r[1]) for r in rows}


def work_left(session: Session, artifact: str, want: dict[str, Any], *, redo: bool,
              away: list[str] | tuple[str, ...] = ()) -> dict[str, float]:
    """What the scheduler has left to make of an artifact kind, over clips in
    sight: those missing it, and when redo, those it's stale on. Not those
    waiting out a failure (or given up), nor those an earlier step it's made
    from was given up on (they can't be made), nor, for a producer that reads
    the originals, those in libraries whose storage is away. {"clips",
    "seconds"}: how many, and their seconds of video."""
    if artifact not in MADE:
        raise KeyError(artifact)
    p = PRODUCERS[artifact]
    made = f"({MADE[artifact]})"
    stale = f" OR ({made} AND (l.asset_id IS NULL OR {_stale_sql()}))" if redo else ""
    stale += f" OR ({made} AND ({RECHECK[artifact]}))"  # rechecks are handed out with what's missing
    blocked = "".join(
        f" AND (({MADE[need]}) OR NOT EXISTS (SELECT 1 FROM artifact_lineage n WHERE n.asset_id = a.asset_id"
        f"   AND n.artifact = '{need}' AND n.retry_at = 'infinity'))" for need in p.needs)
    params = {**_stale_params(artifact, want, None, None), "away": list(away) if p.storage else []}
    row = session.execute(text(
        "SELECT count(*), COALESCE(sum(a.duration_sec), 0)"
        " FROM active_assets a"
        " LEFT JOIN artifact_lineage l ON l.asset_id = a.asset_id AND l.artifact = :artifact"
        f" WHERE {APPLIES[artifact]} AND {_scope_sql()}"
        "   AND (l.retry_at IS NULL OR l.retry_at <= now())"
        "   AND a.library_id <> ALL(CAST(:away AS text[]))"
        f"   AND (NOT {made}{stale}){blocked}"
    ), params).one()
    return {"clips": float(row[0]), "seconds": float(row[1])}


# ---------------------------------------------------------------------------
# Redo on change: a settings change is the approval (Robert, Oct 9)
# ---------------------------------------------------------------------------

# Producers whose artifact can't be made again in place yet, and why: they
# show as stale and wait. These two are Lumiverb's own producers': one added
# (a plug-in's work) can always be made again and uses no AI job.
CANT_REDO: dict[str, str] = {a: p.cant_redo for a, p in PRODUCERS.items() if p.cant_redo}
# What each AI job's model makes (Settings → AI): a new model makes these stale.
JOB_ARTIFACTS: dict[str, tuple[str, ...]] = {
    job: tuple(a for a, p in PRODUCERS.items() if p.job == job) for job in {p.job for p in PRODUCERS.values() if p.job}
}


def redoable(artifact: str) -> bool:
    """Made again in place when made another way than now: what the
    scheduler makes (its redo kind), or a scan (MADE_AT_SCAN: the plan's
    remake, src/server/scans.py), unless it can't be yet (cant_redo)."""
    return (artifact in MISSING_FLAGS.values() or artifact in MADE_AT_SCAN) and artifact not in CANT_REDO


# A lineage row l names the file it was made from, or the clip a's file isn't
# known: unless it does, it's stale however it was made (_stale_sql), so a
# redo and a scan's remake make it again.
NAMES_ITS_FILE = "(a.content_hash IS NULL OR l.source_hash IS NOT NULL)"


def redo_due(artifact: str) -> str:
    """Made, but with another producer, version or settings than now (:p_<a>,
    :v_<a>, :h_<a>), or from a file it doesn't name while the clip's is known
    (made before its hash was: stale, as counts has it), from the file as it
    is: what a redo makes again. A person's is never redone; one missing, or
    made from a file that has since changed, is due as missing instead; a
    failure waits its turn. On active_assets a, after lineage_join()."""
    assert redoable(artifact), artifact
    return (f"(({APPLIES[artifact]}) AND ({MADE[artifact]}) AND NOT {source_changed(artifact)}"
            f" AND NOT {waiting(artifact)} AND NOT ({RECHECK[artifact]})"
            f" AND NOT EXISTS (SELECT 1 FROM artifact_lineage l WHERE l.asset_id = a.asset_id"
            f"   AND l.artifact = '{artifact}' AND (l.producer = :person"
            f"     OR (l.producer = :p_{artifact} AND l.producer_version = :v_{artifact}"
            f"         AND l.settings_hash = :h_{artifact} AND {NAMES_ITS_FILE}))))")


def redo_params(artifact: str, want: dict[str, Any]) -> dict[str, Any]:
    return {"person": PERSON, f"p_{artifact}": want["producer"], f"v_{artifact}": want["version"],
            f"h_{artifact}": want["settings_hash"]}


# A pause's scope: all of a switch's work (Scans, Upkeep or a producer's,
# missing and stale), or a producer's redo alone (its stale clips).
WORK, REDO = "work", "redo"
SCOPES = (WORK, REDO)


@dataclass(frozen=True)
class Pauses:
    """What's paused, by scope: {target: {paused_by, paused_at}} each; the
    libraries whose scans are paused, {library_id: {paused_by, paused_at}}
    (ADR-018 phase 1: a library's own, the libraries' columns); and whether
    Pause all is on (everything: the "all" row), when nothing, the
    scheduler's own look at each library's storage included, touches the
    library drives."""

    work: dict[str, dict[str, Any]] = field(default_factory=dict)
    redo: dict[str, dict[str, Any]] = field(default_factory=dict)
    everything: bool = False
    libraries: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Every library not in the trash, paused or not.
    library_ids: tuple[str, ...] = ()

    def scans_paused(self, library_id: str | None = None) -> bool:
        """Whether the library's scans are paused (its own, or Pause all);
        with no library, whether every library's are (none found anything
        new then)."""
        if library_id is not None:
            return self.everything or library_id in self.libraries
        return self.everything or (bool(self.library_ids) and all(i in self.libraries for i in self.library_ids))


# Pause all is a row of its own (target "all", scope work): every switch,
# those there now and any a later version adds, is paused while it's there.
# An account someone paused stays paused through an update that brings a
# new producer.
ALL = "all"


def pauses(session: Session) -> Pauses:
    """Every pause, in one read: the switches paused (WORK), the producers
    whose redo is stopped (REDO) and the libraries whose scans are paused
    (those in the trash aren't scanned anyway). What's paused starts nothing
    more; what's running finishes. While Pause all is on, every switch there
    is now and every library show as paused, by whoever paused all (unless
    paused alone before), and everything is True."""
    from src.shared.producers import pause_targets

    work: dict[str, dict[str, Any]] = {}
    redo: dict[str, dict[str, Any]] = {}
    for target, scope, by, at in session.execute(text(
            "SELECT target, scope, paused_by, paused_at FROM producer_pauses")):
        (work if scope == WORK else redo)[target] = {"paused_by": by, "paused_at": at}
    everything = work.pop(ALL, None)
    libraries: dict[str, dict[str, Any]] = {}
    library_ids = []
    for library_id, by, at in session.execute(text(
            "SELECT library_id, scans_paused_by, scans_paused_at FROM libraries WHERE status = 'active'")):
        library_ids.append(library_id)
        if at is not None:
            libraries[library_id] = {"paused_by": by, "paused_at": at}
        elif everything is not None:
            libraries[library_id] = everything
    if everything is not None:
        for target in pause_targets():
            work.setdefault(target, everything)
    return Pauses(work=work, redo=redo, everything=everything is not None, libraries=libraries,
                  library_ids=tuple(library_ids))


def pause(session: Session, target: str, scope: str, *, by: str | None) -> None:
    """Pause a switch's work, or stop a producer's redo. Again is fine: who
    paused it first, and when, stays. Doesn't commit."""
    assert scope in SCOPES, scope
    session.execute(text(
        "INSERT INTO producer_pauses (target, scope, paused_by, paused_at) VALUES (:t, :s, :by, :now)"
        " ON CONFLICT (target, scope) DO NOTHING"), {"t": target, "s": scope, "by": by, "now": utcnow()})


def keep_made(session: Session, artifacts: list[str] | tuple[str, ...], *, by: str) -> None:
    """New files only (ADR-018 decision 6): what these producers made
    another way than now stays as it is, by a person's choice, until someone
    remakes it (resumes the redo): each one's redo stopped by them, whoever
    stopped it before (a new version's hold becomes their choice). Doesn't
    commit."""
    kept = [a for a in artifacts if redoable(a)]
    resume(session, kept, REDO)
    for artifact in kept:
        pause(session, artifact, REDO, by=by)


def resume(session: Session, targets: list[str] | tuple[str, ...], scope: str) -> None:
    """Carry on with what was paused, in that scope. Resuming one switch while
    Pause all is on leaves every other switch there is now, and every
    library's scans, paused, each on its own: the account is partly paused
    from then on. Doesn't commit."""
    assert scope in SCOPES, scope
    if not targets:
        return
    if scope == WORK:
        _end_pause_all(session)
    session.execute(text("DELETE FROM producer_pauses WHERE target = ANY(:t) AND scope = :s"),
                    {"t": list(targets), "s": scope})


def _end_pause_all(session: Session) -> None:
    """Pause all (the "all" row) becomes what it paused, each paused on its
    own by whoever paused all: every switch there is now, and every library's
    scans. Nothing when it isn't on. Doesn't commit."""
    from src.shared.producers import pause_targets

    everything = session.execute(text(
        "DELETE FROM producer_pauses WHERE target = :all AND scope = :s RETURNING paused_by, paused_at"
    ), {"all": ALL, "s": WORK}).first()
    if everything is None:
        return
    for target in pause_targets():
        session.execute(text(
            "INSERT INTO producer_pauses (target, scope, paused_by, paused_at) VALUES (:t, :s, :by, :at)"
            " ON CONFLICT (target, scope) DO NOTHING"),
            {"t": target, "s": WORK, "by": everything[0], "at": everything[1]})
    session.execute(text(
        "UPDATE libraries SET scans_paused_at = :at, scans_paused_by = :by WHERE scans_paused_at IS NULL"),
        {"by": everything[0], "at": everything[1]})


def pause_scans(session: Session, library_ids: list[str], *, by: str | None) -> None:
    """Pause these libraries' scans: no new or changed files are found in
    them (ADR-018 phase 1). Again is fine: who paused first, and when, stays.
    Doesn't commit."""
    session.execute(text(
        "UPDATE libraries SET scans_paused_at = :now, scans_paused_by = :by"
        " WHERE library_id = ANY(:ids) AND scans_paused_at IS NULL"),
        {"ids": list(library_ids), "by": by, "now": utcnow()})


def resume_scans(session: Session, library_ids: list[str]) -> None:
    """Carry on scanning these libraries. While Pause all is on, everything
    else stays paused, each on its own (_end_pause_all). Doesn't commit."""
    if not library_ids:
        return
    _end_pause_all(session)
    session.execute(text(
        "UPDATE libraries SET scans_paused_at = NULL, scans_paused_by = NULL WHERE library_id = ANY(:ids)"),
        {"ids": list(library_ids)})


def pause_everything(session: Session, *, by: str | None) -> None:
    """Pause all: every switch's work paused, now and any added later (the
    "all" row; a switch paused alone before keeps who paused it). Doesn't commit."""
    pause(session, ALL, WORK, by=by)


def resume_everything(session: Session) -> None:
    """Resume all: every switch, and every library's scans, running again (a
    stopped redo stays stopped). Doesn't commit."""
    session.execute(text("DELETE FROM producer_pauses WHERE scope = :s"), {"s": WORK})
    session.execute(text(
        "UPDATE libraries SET scans_paused_at = NULL, scans_paused_by = NULL WHERE scans_paused_at IS NOT NULL"))


_VERSION_SEEN = "producer.{}.version_seen"


def hold_new_version(session: Session, artifact: str) -> bool:
    """A producer's new version is like new settings that nobody approved:
    the first time it's seen (per account) with clips made by an older
    version of the same producer, its redo is stopped (paused_by None) until
    an admin resumes it, or its settings change. Seen once, it's not asked
    again (resuming sticks). True when it stopped the redo just now. Commits."""
    p = PRODUCERS[artifact]
    key = _VERSION_SEEN.format(artifact)
    if _meta(session, key) == p.version:
        return False
    older = _made_by_older_version(session, artifact)
    if older:
        pause(session, artifact, REDO, by=None)
    session.execute(text(
        "INSERT INTO system_metadata (key, value, updated_at) VALUES (:k, :v, now())"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"), {"k": key, "v": p.version})
    session.commit()
    return older


def new_version_unseen(session: Session, artifact: str) -> bool:
    """hold_new_version would stop the producer's redo now: its version not
    seen yet, with clips made by an older one. Only reads (a GET's own)."""
    p = PRODUCERS[artifact]
    return _meta(session, _VERSION_SEEN.format(artifact)) != p.version and _made_by_older_version(session, artifact)


def _made_by_older_version(session: Session, artifact: str) -> bool:
    p = PRODUCERS[artifact]
    return session.execute(text(
        "SELECT 1 FROM artifact_lineage WHERE artifact = :a AND producer = :p AND producer_version <> :v LIMIT 1"
    ), {"a": artifact, "p": p.producer, "v": p.version}).first() is not None


def upkeep_paused(session: Session) -> bool:
    """The Upkeep switch is paused: the trash purge, file cleanup and spreading
    face names wait; search sync goes on, so the site keeps up with edits."""
    from src.shared.producers import PAUSE_UPKEEP

    return session.execute(text("SELECT 1 FROM producer_pauses WHERE target IN (:t, :all) AND scope = :s"),
                           {"t": PAUSE_UPKEEP, "all": ALL, "s": WORK}).first() is not None


def would_redo(session: Session, artifact: str, want: dict[str, Any]) -> int:
    """Clips in sight whose artifact would be made again if it were made as
    want says (desired): those a producer made another way. What the 409
    before a new model or new settings counts."""
    return counts(session, artifact, want)["stale"] if redoable(artifact) else 0


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
           *, outcome: str = "ok", source_hash: str | None = None, person: bool = False,
           produced_at: datetime | None = None, commit: bool = True) -> None:
    """Record how an artifact was just made. A write that doesn't say (an
    old client, the macOS app today) is an unknown producer's: stale, so
    the server makes it again. person: a person made it (only the server
    says so; a client can't). The source (lineage's source_hash, a content
    hash) defaults to the clip's file now; one given as its file's whole
    "sha256", made before fp1, is the clip's fp1 once a scan swapped them
    (src/server/scans.py rekey): a clip's content
    hash changes only that way (another file is another clip), so work
    handed out before the swap is still of this content.
    produced_at: when it was made, for one kept and shown again (now otherwise)."""
    lineage = lineage or {}
    producer = PERSON if person else str(lineage.get("producer") or UNKNOWN)
    if not person and producer != PRODUCERS[artifact].producer:
        # Another kind's producer can't make this one, and a client can't
        # claim a person made it: whatever it is, it isn't current.
        producer = UNKNOWN
    source = lineage.get("source_hash") or source_hash
    # The level it was made at, for a producer with a level setting (kept apart from the hash).
    setting, level = PRODUCERS[artifact].level, lineage.get("level")
    if producer in (UNKNOWN, PERSON) or setting is None or isinstance(level, bool) \
            or not isinstance(level, int) or not 0 <= level < len(setting.choices):
        level = None
    session.execute(text(
        "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
        " source_hash, produced_at, outcome, error, attempts, retry_at, level)"
        " VALUES (:a, :artifact, :producer, :version, :hash,"
        "         COALESCE((SELECT c.content_hash FROM assets c WHERE c.asset_id = :a"
        f"                   AND starts_with(c.content_hash, '{FP1}') AND c.file_size > {FULL_UP_TO}"
        f"                   AND starts_with(CAST(:source AS text), '{SHA256}')),"
        "                  :source, (SELECT content_hash FROM assets WHERE asset_id = :a)),"
        "         :now, :outcome, NULL, 0, NULL,"
        "         :level)"
        " ON CONFLICT (asset_id, artifact) DO UPDATE SET producer = EXCLUDED.producer,"
        "   producer_version = EXCLUDED.producer_version, settings_hash = EXCLUDED.settings_hash,"
        "   source_hash = EXCLUDED.source_hash, produced_at = EXCLUDED.produced_at,"
        "   outcome = EXCLUDED.outcome, error = NULL, attempts = 0, retry_at = NULL, level = EXCLUDED.level"
    ), {"a": asset_id, "artifact": artifact, "producer": producer, "level": level,
        "version": "" if producer in (UNKNOWN, PERSON) else str(lineage.get("version") or ""),
        "hash": "" if producer in (UNKNOWN, PERSON) else str(lineage.get("settings_hash") or ""),
        "source": source, "now": produced_at or utcnow(), "outcome": outcome})
    _forget_crashes(session, asset_id, artifact)
    if commit:
        session.commit()


def forget(session: Session, asset_ids: list[str], artifact: str, *, commit: bool = True) -> None:
    """The artifact is gone (a transcript deleted, scenes reset): missing again."""
    if asset_ids:
        session.execute(text("DELETE FROM artifact_lineage WHERE asset_id = ANY(:ids) AND artifact = :artifact"),
                        {"ids": asset_ids, "artifact": artifact})
        if commit:
            session.commit()


def lock_settings(session: Session, artifact: str) -> None:
    """One change of a producer's settings at a time (two saves at once mustn't
    lose either's), until the transaction ends."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"producer.{artifact}"})


def failing_since(session: Session, since) -> int:
    """Clip-steps in sight whose last try failed since `since`."""
    return session.execute(text(
        "SELECT count(*) FROM artifact_lineage l JOIN active_assets a ON a.asset_id = l.asset_id"
        " WHERE l.error IS NOT NULL AND l.failed_at > :since"), {"since": since}).scalar() or 0


def producer_of(session: Session, asset_id: str, artifact: str) -> str | None:
    """Whose the clip's artifact is: its producer's name (a person's: PERSON), or None when it has none."""
    return session.execute(text("SELECT producer FROM artifact_lineage WHERE asset_id = :a AND artifact = :t"),
                           {"a": asset_id, "t": artifact}).scalar()


def made_from(artifact: str) -> tuple[str, ...]:
    """The artifact and what goes with it when it's made again: its producer's
    redo_also, and theirs (src/producers/<artifact>/), in that order."""
    out = [artifact]
    for a in out:
        out.extend(b for b in PRODUCERS[a].redo_also if b not in out)
    return tuple(out)


def start_over(session: Session, asset_ids: list[str], artifact: str) -> None:
    """The clips' artifact is made again from the start: its lineage goes, with
    that of what's made from it (made_from), so each is missing again. Doesn't
    commit (the caller drops the artifacts themselves in the same transaction)."""
    if asset_ids:
        session.execute(text("DELETE FROM artifact_lineage WHERE asset_id = ANY(:ids) AND artifact = ANY(:artifacts)"),
                        {"ids": list(asset_ids), "artifacts": list(made_from(artifact))})


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
        " source_hash, produced_at, outcome, error, attempts, retry_at, failed_at)"
        " VALUES (:a, :artifact, '', '', '', NULL, :now, 'failed', :error, :attempts,"
        "         CASE WHEN :give_up THEN 'infinity'::timestamptz ELSE :retry END, :now)"
        " ON CONFLICT (asset_id, artifact) DO UPDATE SET error = EXCLUDED.error,"
        "   attempts = EXCLUDED.attempts, retry_at = EXCLUDED.retry_at, failed_at = EXCLUDED.failed_at"
    ), {"a": asset_id, "artifact": artifact, "now": now, "error": error[:2000], "attempts": attempts,
        "retry": now + wait, "give_up": attempts >= GIVE_UP_AFTER})
    _forget_crashes(session, asset_id, artifact)
    if commit:
        session.commit()


def _forget_crashes(session: Session, asset_id: str, artifact: str) -> None:
    session.execute(text("DELETE FROM producer_crashes WHERE asset_id = :a AND artifact = :artifact"),
                    {"a": asset_id, "artifact": artifact})


def note_crashes(session: Session, artifact: str, asset_ids: list[str], error: str, *,
                 limit: int = CRASHES_BEFORE_CHARGE) -> list[str]:
    """These clips' jobs crashed, uncharged: one more in a row for each.
    Returns those that reached limit (their count starts over): the caller
    charges them a failure. Clips that are gone are left out. Commits."""
    if not asset_ids:
        return []
    rows = session.execute(text(
        "INSERT INTO producer_crashes (asset_id, artifact, crashes, error, updated_at)"
        " SELECT asset_id, :artifact, 1, :error, now() FROM assets WHERE asset_id = ANY(:ids)"
        " ON CONFLICT (asset_id, artifact) DO UPDATE SET crashes = producer_crashes.crashes + 1,"
        "   error = EXCLUDED.error, updated_at = EXCLUDED.updated_at"
        " RETURNING asset_id, crashes"
    ), {"artifact": artifact, "error": error[:2000], "ids": list(dict.fromkeys(asset_ids))}).all()
    reached = sorted(r[0] for r in rows if r[1] >= limit)
    if reached:
        session.execute(text("DELETE FROM producer_crashes WHERE artifact = :artifact AND asset_id = ANY(:ids)"),
                        {"artifact": artifact, "ids": reached})
    session.commit()
    return reached


def failures(session: Session, *, artifact: str | None = None, library_id: str | None = None,
             asset_ids: list[str] | None = None, limit: int = 50, after: str | None = None,
             within: list[str] | None = None) -> list[dict[str, Any]]:
    """Clips in sight whose last try failed, newest failure first: why, how
    many tries, and when the next one is (None when given up). after: the
    cursor the last page ended with; within: only these libraries."""
    rows = session.execute(text(
        "SELECT l.asset_id, l.artifact, l.error, l.attempts, l.failed_at, l.retry_at,"
        " a.rel_path, a.library_id, a.media_type, lib.name AS library_name"
        " FROM artifact_lineage l JOIN active_assets a ON a.asset_id = l.asset_id"
        " JOIN libraries lib ON lib.library_id = a.library_id"
        " WHERE l.error IS NOT NULL"
        "   AND (CAST(:artifact AS text) IS NULL OR l.artifact = :artifact)"
        "   AND (CAST(:lib AS text) IS NULL OR a.library_id = :lib)"
        "   AND (CAST(:ids AS text[]) IS NULL OR a.asset_id = ANY(CAST(:ids AS text[])))"
        f"  AND {_WITHIN}"
        "   AND (CAST(:after AS text) IS NULL"
        "        OR (COALESCE(l.failed_at, l.produced_at), l.asset_id || '/' || l.artifact)"
        "           < (CAST(split_part(:after, '|', 1) AS timestamptz), split_part(:after, '|', 2)))"
        " ORDER BY COALESCE(l.failed_at, l.produced_at) DESC, l.asset_id || '/' || l.artifact DESC"
        " LIMIT :n"
    ), {"artifact": artifact, "lib": library_id, "ids": asset_ids, "after": after, "n": limit,
        "within": within}).mappings().all()
    out = []
    for r in rows:
        given_up = r["retry_at"] is not None and r["retry_at"].year >= 9999
        when = r["failed_at"]
        out.append({**r, "given_up": given_up, "retry_at": None if given_up else r["retry_at"],
                    "cursor": f"{when.isoformat() if when else ''}|{r['asset_id']}/{r['artifact']}"})
    return out


def retry(session: Session, *, artifact: str | None = None, library_id: str | None = None,
          asset_ids: list[str] | None = None, within: list[str] | None = None) -> int:
    """Try these failing clips again now, given up or not (the back-off starts
    over). None for a part means every one (the caller has made sure it was
    named so); within: only these libraries. Doesn't commit. Returns how many."""
    r = session.execute(text(
        "UPDATE artifact_lineage l SET retry_at = NULL, attempts = 0 FROM active_assets a"
        " WHERE a.asset_id = l.asset_id AND l.error IS NOT NULL"
        "   AND (CAST(:artifact AS text) IS NULL OR l.artifact = :artifact)"
        "   AND (CAST(:lib AS text) IS NULL OR a.library_id = :lib)"
        "   AND (CAST(:ids AS text[]) IS NULL OR l.asset_id = ANY(CAST(:ids AS text[])))"
        f"  AND {_WITHIN}"
    ), {"artifact": artifact, "lib": library_id, "ids": asset_ids, "within": within})
    if r.rowcount:
        nudge(session)
    return int(r.rowcount or 0)


def nudge(session: Session) -> None:
    """Have the scheduler look again now: it lets go of the hour it holds
    clips it just tried, and lists what's due at once. Doesn't commit."""
    session.execute(text(
        "INSERT INTO system_metadata (key, value, updated_at) VALUES ('scheduler.retry_at', :now, now())"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
        {"now": utcnow().isoformat()})


def redo_below(session: Session, artifact: str, want: dict[str, Any], *, library_id: str | None = None,
               asset_ids: list[str] | None = None) -> int:
    """Make again the clips' artifact made below the level now (counts'
    below), and only those: among asset_ids, or every such clip in sight
    (in one library when given) when asset_ids is None; the caller has made
    sure the target was named, never inferred. As ask_redo, their lineage
    stops being current, so the scheduler's redo makes them again after
    anything missing (a stopped redo or a pause holds them like any redo),
    at the level then. Doesn't commit. Returns how many."""
    assert redoable(artifact) and PRODUCERS[artifact].level is not None, artifact
    r = session.execute(text(
        "UPDATE artifact_lineage l SET settings_hash = '' FROM active_assets a"
        " WHERE a.asset_id = l.asset_id AND l.artifact = :artifact"
        f"   AND ({APPLIES[artifact]}) AND ({MADE[artifact]}) AND {_below_sql(artifact)}"
        f"   AND {_scope_sql()}"
    ), _stale_params(artifact, want, library_id, asset_ids))
    return int(r.rowcount or 0)


def ask_redo(session: Session, artifact: str, library_id: str | None = None) -> int:
    """Make the clips' artifact again (in one library, or all when None): its
    lineage stops being current, as if made with other settings, so the
    scheduler's redo makes it again after anything missing (and what's made
    from it with it, as a redo does). A person's is never touched; one
    already stale stays as it is. Doesn't commit. Returns how many."""
    assert redoable(artifact), artifact
    r = session.execute(text(
        "UPDATE artifact_lineage l SET settings_hash = '' FROM active_assets a"
        " WHERE a.asset_id = l.asset_id AND l.artifact = :artifact AND l.settings_hash <> ''"
        "   AND l.producer NOT IN ('', :person, :unknown)"
        "   AND (CAST(:lib AS text) IS NULL OR a.library_id = :lib)"
    ), {"artifact": artifact, "lib": library_id, "person": PERSON, "unknown": UNKNOWN})
    return int(r.rowcount or 0)
