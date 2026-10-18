"""What a scan found, compared with the library on the server (ADR-018 decision 12).

The client says what it found; the server decides. A scan is a short-lived
session (routers/scans.py): the client starts it, sends every media file its
walk saw (path, size, modified time), says when the listing is complete, and
gets the plan: what to upload now, what to hash first, what a person removed,
how many are unchanged and how many clips are gone. It sends the hashes the
plan asks for and gets a decision for each; then it finishes, and what's gone
is archived as missing. Until then it can also list the files it skips
because a person removed them (and how), and the clips gone (skipped_page,
gone_page: what `lumiverb scan --explain` shows).

The rules are today's scanner's (src/processing/scan.py), each moved here:
- unchanged when size and modified time match the clip at the path; hashed
  when they don't, or for --thorough; uploaded for --force, and when a
  preview the scan makes is missing: never arrived, or lost (its reader's
  work, src/server/reader_work.py);
- content is known by its content hash (src/shared/content_hash.py): a file
  over 64 MiB by an fp1 of 3 MiB of it. A clip whose hash was made before
  fp1 (the whole file's) is hashed again when it's unchanged, and its hash
  swapped (rekey); until then a file its size is hashed whole too, so it's
  compared with what was (legacy);
- a file a person trashed, archived or deleted for good is skipped when its
  size and time are the ones removed, or its content is (_skipped); unless
  the scan adds them anyway (force_add, `lumiverb scan --force-add`, a
  person's last resort): then a clip trashed or archived with this content
  at this path comes back into sight, and a file deleted for good comes
  back as a new clip and is forgotten as deleted;
- a move is a new path whose content is a clip gone from this scan, never a
  copy (that clip's file still there); only files of a size a gone clip has
  are hashed for it (_detect_moves);
- gone is a clip of the scan's folder and media type the scan didn't find
  (_detect_deletions), never under a folder it couldn't list, and never one
  made or changed since the scan started: another scan may have just made it.

Every comparison is one statement over the whole listing, the found files
joined with the library's clips, so planning 100,000 files is one round
trip, and a batch of hashes a fixed handful.

A client whose answer was lost (a dropped connection, the API restarting)
sends the call again, so every call but the start is safe to repeat: files
found take their latest size and time, a plan sent again for the same
unlisted folders is the same plan, hashes keep their decisions, and moves
and the finish give the answers they kept (ScanSession.answers). Scans a
killed client left behind expire a day after their last call, and upkeep
deletes them (delete_expired).
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from ulid import ULID

from src.server.models.tenant import ScanSession
from src.server.reader_work import previews_missing
from src.server.repository import lineage
from src.server.repository.assets import AssetRepository
from src.shared.content_hash import FULL_UP_TO, SHA256, sampled, whole_file
from src.shared.file_extensions import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from src.shared.producers import MADE_AT_SCAN
from src.shared.utils import utcnow

# An unfinished scan is deleted a day after its last call.
TTL = timedelta(hours=24)
# Files in one call (found, hashes); a plan's pages are MAX_PAGE (limits.py), as every page of clips.
MAX_FILES = 1000
# Unchanged files a scan uploads again, at most, to remake their previews (ADR-018 decision 6: after
# everything else). A few at each scan, so no scan is long for them: new files a later scan finds wait
# at most this many behind them.
REMAKES_PER_SCAN = 250

ACTIONS = ("upload", "hash", "unchanged", "skipped")
# What a plan's pages list unless asked: what the client acts on.
TO_ACT_ON = ("upload", "hash", "skipped")
# How a person removed a file (REMOVED), in the order one is named when
# several removals at a path are the file's (skipped_page).
REMOVED_AS = ("trash", "archived", "deleted")


class NotPlannedError(Exception):
    """Hashes sent for files the plan doesn't list (not found, or present
    only: settling, empty, another media type)."""

    def __init__(self, rel_paths: list[str]) -> None:
        super().__init__(f"{len(rel_paths)} file(s) the plan doesn't list")
        self.rel_paths = rel_paths


class PathTakenError(Exception):
    """A move's new path was taken by another clip meanwhile (another scan's ingest)."""


@dataclass
class Found:
    rel_path: str
    file_size: int
    file_mtime: datetime | None
    media_type: str
    settling: bool


@dataclass
class Decided:
    rel_path: str
    content_hash: str
    decision: str  # unchanged | upload | moved | move_candidate | move_skipped | skipped | restored
    asset_id: str | None = None
    moved_from: str | None = None
    previous_hash: str | None = None


@dataclass(frozen=True)
class Hashed:
    """A file's content hash as a scan sent it, with its whole file's
    "sha256" when the plan asked for that too (legacy): previous_hash."""

    rel_path: str
    content_hash: str
    previous_hash: str | None = None


def media_type_of(rel_path: str) -> str | None:
    """image or video, by the file's extension (src/shared/file_extensions.py); None for neither."""
    ext = posixpath.splitext(rel_path)[1].lower()
    return "video" if ext in VIDEO_EXTENSIONS else "image" if ext in IMAGE_EXTENSIONS else None


def in_scope(rel_path: str, scope: str) -> bool:
    """A file in the scan's folder ("" is the whole library)."""
    return not scope or rel_path.startswith(scope + "/")


def _whole_file(alias: str) -> str:
    """SQL: the row's content hash was made before fp1, of a file fp1 hashes
    now (content_hash.whole_file)."""
    return f"({alias}.file_size > {FULL_UP_TO} AND starts_with({alias}.content_hash, '{SHA256}'))"


# Clips of the scan's library, folder and media type that it didn't find: in
# sight, at no path it found, under no folder it couldn't list, made and last
# changed before it started, and not claimed by one of its moves.
GONE = """
    SELECT a.asset_id, a.rel_path, a.content_hash, a.file_size, a.media_type
    FROM assets a
    WHERE a.library_id = :library_id AND a.deleted_at IS NULL
      AND (CAST(:scope AS text) = '' OR starts_with(a.rel_path, CAST(:scope AS text) || '/'))
      AND (CAST(:media_type AS text) = 'all' OR a.media_type = CAST(:media_type AS text))
      AND a.created_at <= :started_at AND a.updated_at <= :started_at
      AND NOT EXISTS (SELECT 1 FROM scan_found f WHERE f.scan_id = :scan_id AND f.rel_path = a.rel_path)
      AND NOT EXISTS (SELECT 1 FROM unnest(CAST(:unlisted AS text[])) AS u(folder)
                      WHERE u.folder = '' OR a.rel_path = u.folder OR starts_with(a.rel_path, u.folder || '/'))
      AND NOT EXISTS (SELECT 1 FROM scan_found m WHERE m.scan_id = :scan_id AND m.asset_id = a.asset_id
                      AND m.decision IN ('moved', 'move_candidate', 'move_skipped'))
"""

# Files a person removed from the library, with each one's content, size and
# time, and how (REMOVED_AS): in the trash or archived by a person (their
# clips), or deleted for good (ignored_files: no clip). A scan skips them; no
# client is told the list, only which of the files it found are these
# (skipped_page).
REMOVED = """
    SELECT rel_path, content_hash, file_size, file_mtime, asset_id,
           CASE deleted_reason WHEN 'user' THEN 'trash' ELSE 'archived' END AS removed_as
    FROM assets
    WHERE library_id = :library_id AND deleted_at IS NOT NULL AND deleted_reason IN ('user', 'archived')
    UNION ALL
    SELECT rel_path, content_hash, file_size, file_mtime, NULL, 'deleted' FROM ignored_files WHERE library_id = :library_id
"""

# Each found file's plan, as "action/reason". The clip in sight at the path
# comes first (a removed file's path is never one a clip holds: that file is
# the clip's). Settling and empty files, and those of another media type, are
# present but get no plan. An unchanged clip whose hash was made before fp1
# is hashed again (rekey: its fp1 takes the place of its whole file's hash).
# A file to hash or upload whose size a clip or file deleted for good of the
# library has, its hash made before fp1, is hashed whole too (legacy): it's
# compared with what was. An unchanged clip whose previews were made another
# way than now is uploaded again (remake: ADR-018 decision 6), while their
# remakes run (:remake_<artifact>), REMAKES_PER_SCAN at most (:remake_cap,
# the oldest clips first), its previews made at the account's settings.
_REMAKE = " OR ".join(f"(:remake_{a} AND {lineage.redo_due(a)})" for a in MADE_AT_SCAN)
_PLAN = f"""
    WITH gone AS ({GONE}),
    whole_sizes AS (
        SELECT a.file_size FROM assets a WHERE a.library_id = :library_id AND {_whole_file('a')}
        UNION
        SELECT i.file_size FROM ignored_files i WHERE i.library_id = :library_id AND {_whole_file('i')}
    ),
    removed AS (
        SELECT f.rel_path,
               bool_or(r.content_hash IS NULL) AS unknown,
               COALESCE(bool_or(r.file_size = f.file_size AND r.file_mtime = f.file_mtime), false) AS as_removed
        FROM scan_found f JOIN ({REMOVED}) r ON r.rel_path = f.rel_path
        WHERE f.scan_id = :scan_id
        GROUP BY f.rel_path
    ),
    planned AS (
        SELECT f.rel_path, a.asset_id,
               CASE
                   WHEN f.settling OR f.file_size = 0
                        OR (CAST(:media_type AS text) <> 'all' AND f.media_type <> CAST(:media_type AS text))
                       THEN NULL
                   WHEN a.asset_id IS NOT NULL THEN CASE
                       WHEN {previews_missing()} THEN 'upload/preview_missing'
                       WHEN :force THEN 'upload/force'
                       WHEN :thorough THEN 'hash/thorough'
                       WHEN a.file_size = f.file_size AND a.file_mtime = f.file_mtime
                           THEN CASE WHEN {_whole_file('a')} THEN 'hash/rekey'
                                     WHEN {_REMAKE} THEN 'upload/remake'
                                     ELSE 'unchanged/' END
                       ELSE 'hash/changed' END
                   WHEN (r.unknown OR r.as_removed) AND NOT :force_add THEN 'skipped/removed'
                   WHEN r.rel_path IS NOT NULL THEN 'hash/removed'
                   WHEN :moves AND f.file_size IN (SELECT g.file_size FROM gone g WHERE g.content_hash IS NOT NULL)
                       THEN 'hash/move'
                   ELSE 'upload/new'
               END AS kind
        FROM scan_found f
        LEFT JOIN assets a ON a.library_id = :library_id AND a.rel_path = f.rel_path AND a.deleted_at IS NULL
        {lineage.LINEAGE_JOIN}
        LEFT JOIN removed r ON r.rel_path = f.rel_path
        WHERE f.scan_id = :scan_id
    ),
    capped AS (
        SELECT p.rel_path, p.asset_id,
               CASE WHEN p.kind = 'upload/remake' AND row_number() OVER (
                             PARTITION BY p.kind = 'upload/remake' ORDER BY p.asset_id COLLATE "C") > :remake_cap
                    THEN 'unchanged/' ELSE p.kind END AS kind
        FROM planned p
    )
    UPDATE scan_found f
    SET action = split_part(p.kind, '/', 1), reason = NULLIF(split_part(p.kind, '/', 2), ''),
        asset_id = p.asset_id,
        legacy = split_part(p.kind, '/', 1) IN ('upload', 'hash') AND p.kind <> 'hash/rekey'
                 AND f.file_size IN (SELECT w.file_size FROM whole_sizes w)
    FROM capped p
    WHERE f.scan_id = :scan_id AND f.rel_path = p.rel_path AND p.kind IS NOT NULL
"""

# Hashes sent, in order, with what the scan and the library know of each path now.
_POSTED = f"""
    WITH posted AS (
        SELECT * FROM unnest(CAST(:paths AS text[]), CAST(:hashes AS text[])) WITH ORDINALITY
                      AS p(rel_path, content_hash, ord)
    )
    SELECT p.rel_path, p.content_hash, f.action, f.reason, f.decision, f.asset_id AS decided_id, f.moved_from,
           f.file_size, f.file_mtime,
           a.asset_id, a.content_hash AS asset_hash,
           COALESCE(a.file_size <> f.file_size OR a.file_mtime IS DISTINCT FROM f.file_mtime, false) AS stat_differs,
           COALESCE({previews_missing()}, false) AS preview_missing,
           -- A person removed this content here, or a file here whose content was never known.
           p.rel_path IN (SELECT r.rel_path FROM ({REMOVED}) r
                          JOIN posted q ON q.rel_path = r.rel_path
                                         AND (q.content_hash = r.content_hash OR r.content_hash IS NULL))
               AS removed,
           -- Its clip, when it's in the trash or archived: the latest removed.
           (SELECT c.asset_id FROM assets c
            WHERE c.library_id = :library_id AND c.rel_path = p.rel_path AND c.deleted_at IS NOT NULL
              AND c.deleted_reason IN ('user', 'archived')
              AND (c.content_hash = p.content_hash OR c.content_hash IS NULL)
            ORDER BY c.deleted_at DESC, c.asset_id COLLATE "C" LIMIT 1) AS removed_clip
    FROM posted p
    LEFT JOIN scan_found f ON f.scan_id = :scan_id AND f.rel_path = p.rel_path
    LEFT JOIN assets a ON a.library_id = :library_id AND a.rel_path = p.rel_path AND a.deleted_at IS NULL
    ORDER BY p.ord
"""


def _params(scan: ScanSession) -> dict:
    return {"scan_id": scan.scan_id, "library_id": scan.library_id, "scope": scan.scope,
            "media_type": scan.media_type, "started_at": scan.started_at, "unlisted": list(scan.unlisted or [])}


def delete_expired(session: Session) -> int:
    """Delete the scans expired by now, with their listings (no commit): how
    many. A client killed mid-scan never comes back to drop its own."""
    return session.execute(text("DELETE FROM scan_sessions WHERE expires_at < :now"), {"now": utcnow()}).rowcount


def start(session: Session, library_id: str, *, scope: str, media_type: str, thorough: bool, force: bool,
          moves: str, dry_run: bool, force_add: bool = False) -> ScanSession:
    """A new scan, started now. Scans expired by now go first, with their listings."""
    delete_expired(session)
    now = utcnow()
    scan = ScanSession(scan_id="scan_" + str(ULID()), library_id=library_id, scope=scope, media_type=media_type,
                       thorough=thorough, force=force, moves=moves, dry_run=dry_run, force_add=force_add,
                       started_at=now, expires_at=now + TTL)
    session.add(scan)
    session.commit()
    session.refresh(scan)
    return scan


def lock(session: Session, scan_id: str) -> ScanSession | None:
    """The scan, locked until the transaction ends: its calls take turns."""
    return session.exec(select(ScanSession).where(ScanSession.scan_id == scan_id).with_for_update()).first()


def answered(scan: ScanSession, call: str) -> dict | None:
    """What the scan answered to a call ("plan": its counts; "moves"; "finish"), or None."""
    return (scan.answers or {}).get(call)


def keep_answer(session: Session, scan: ScanSession, call: str, answer: dict) -> None:
    """Keep what the scan answered to a call (no commit): the call sent again,
    its answer lost on the way back, gets this one."""
    scan.answers = {**(scan.answers or {}), call: answer}  # a new dict, so it's saved
    session.add(scan)


def keep_alive(session: Session, scan: ScanSession, now: datetime) -> None:
    """A call extends the scan's life (TTL from now; not committed)."""
    scan.expires_at = now + TTL
    session.add(scan)


def drop(session: Session, scan_id: str) -> bool:
    """Delete a scan and its listing (no commit). False when there's none."""
    return bool(session.execute(text("DELETE FROM scan_sessions WHERE scan_id = :s"), {"s": scan_id}).rowcount)


def add_found(session: Session, scan_id: str, files: list[Found]) -> None:
    """Append files to the scan's listing, one statement. A path sent again
    takes its latest size and time."""
    latest = {f.rel_path: f for f in files}
    session.execute(text("""
        INSERT INTO scan_found (scan_id, rel_path, file_size, file_mtime, media_type, settling)
        SELECT :scan_id, f.rel_path, f.file_size, f.file_mtime, f.media_type, f.settling
        FROM unnest(CAST(:paths AS text[]), CAST(:sizes AS bigint[]), CAST(:mtimes AS timestamptz[]),
                    CAST(:types AS text[]), CAST(:settling AS boolean[]))
             AS f(rel_path, file_size, file_mtime, media_type, settling)
        ON CONFLICT (scan_id, rel_path) DO UPDATE
        SET file_size = EXCLUDED.file_size, file_mtime = EXCLUDED.file_mtime,
            media_type = EXCLUDED.media_type, settling = EXCLUDED.settling
    """), {"scan_id": scan_id, "paths": list(latest), "sizes": [f.file_size for f in latest.values()],
           "mtimes": [f.file_mtime for f in latest.values()], "types": [f.media_type for f in latest.values()],
           "settling": [f.settling for f in latest.values()]})


def plan(session: Session, scan: ScanSession, unlisted: list[str], *, follow_moves: bool,
         remakes: bool = True) -> None:
    """The listing is complete: plan every found file, one statement. Moves
    are looked for when the account follows them, unless the scan forces
    every file or skips moves (as today's scanner: no move detection under
    --force or --skip-moves). Previews made another way than now are
    remade (_remakes), never by a dry run, nor when the reader holds its
    remakes back (remakes False)."""
    scan.unlisted = unlisted
    scan.planned_at = utcnow()
    session.add(scan)
    session.flush()
    session.execute(text(_PLAN), {**_params(scan), "force": scan.force, "thorough": scan.thorough,
                                  "force_add": scan.force_add, "moves": moves_possible(scan, follow_moves),
                                  **_remakes(session, scan, wanted=remakes)})


def _remakes(session: Session, scan: ScanSession, *, wanted: bool = True) -> dict:
    """The plan's word on remaking previews (ADR-018 decision 6): each scan-
    made artifact's remakes run unless a person kept them (its redo stopped)
    or a new version of it waits for an admin (lineage.hold_new_version),
    with what's wanted now; REMAKES_PER_SCAN of them, none in a dry run or
    when the reader holds them back (wanted False)."""
    paused = lineage.pauses(session)
    go = wanted and not scan.dry_run
    out: dict = {"remake_cap": REMAKES_PER_SCAN if go else 0}
    for artifact in MADE_AT_SCAN:
        out[f"remake_{artifact}"] = go and artifact not in paused.redo \
            and not lineage.new_version_unseen(session, artifact)
        out.update(lineage.redo_params(artifact, lineage.desired(session, artifact)))
    return out


def moves_possible(scan: ScanSession, follow_moves: bool) -> bool:
    return follow_moves and not scan.force and scan.moves != "skip"


def counts(session: Session, scan: ScanSession) -> dict[str, int]:
    """The plan's counts: found, each action, settling, empty, and the clips gone."""
    row = session.execute(text("""
        SELECT count(*) AS found,
               count(*) FILTER (WHERE action = 'upload') AS upload,
               count(*) FILTER (WHERE action = 'hash') AS hash,
               count(*) FILTER (WHERE action = 'unchanged') AS unchanged,
               count(*) FILTER (WHERE action = 'skipped') AS skipped,
               count(*) FILTER (WHERE settling) AS settling,
               count(*) FILTER (WHERE file_size = 0) AS empty
        FROM scan_found WHERE scan_id = :scan_id
    """), {"scan_id": scan.scan_id}).mappings().one()
    gone = session.execute(text(f"SELECT count(*) FROM ({GONE}) g"), _params(scan)).scalar_one()
    return {**row, "gone": gone}


def page(session: Session, scan_id: str, actions: tuple[str, ...], after: str | None, limit: int) -> list[dict]:
    """A page of the plan, by rel_path: each file's action, reason and clip,
    and whether to send the whole file's "sha256" too (legacy)."""
    return [dict(r) for r in session.execute(text("""
        SELECT rel_path, action, reason, asset_id, legacy FROM scan_found
        WHERE scan_id = :scan_id AND action = ANY(CAST(:actions AS text[]))
          AND (CAST(:after AS text) IS NULL OR rel_path > :after)
        ORDER BY rel_path LIMIT :limit
    """), {"scan_id": scan_id, "actions": list(actions), "after": after, "limit": limit}).mappings()]


def decide(session: Session, scan: ScanSession, posted: list[Hashed], *,
           follow_moves: bool) -> list[Decided]:
    """Decide each file from its content, in the order sent (no commit):

    - at a path a clip holds: uploaded when its content differs (or the scan
      forces, or a preview the scan makes is missing), else unchanged, and its
      new size and time are recorded so the next scan's check matches;
    - at a new path: skipped when a person removed this content here, unless
      the scan adds them anyway (force_add): then restored, its clip in the
      trash or archived back in sight, or else uploaded, a new clip, and
      forgotten as deleted for good; moved when it's the content of a clip
      gone from this scan, the first such clip by id for the first such file
      sent, applied now when the scan applies moves (apply_moves), a
      candidate when it asks first; else uploaded.

    Hashes made before fp1 are swapped for the fp1s sent first (rekey), so
    each comparison is of two fp1s.

    A dry run decides and changes nothing. A file already moved, skipped or
    restored keeps its decision. Raises NotPlannedError, before anything
    changes, for files the plan doesn't list; PathTakenError when another
    clip took a move's path meanwhile (send the hashes again: the file is
    then that clip's)."""
    params = {**_params(scan), "paths": [h.rel_path for h in posted], "hashes": [h.content_hash for h in posted]}
    rows = session.execute(text(_POSTED), params).mappings().all()
    unknown = [r["rel_path"] for r in rows if r["action"] is None]
    if unknown:
        raise NotPlannedError(unknown)
    swaps = _swaps(rows, {h.rel_path: h.previous_hash for h in posted if h.previous_hash})
    # A dry run's swaps last only while it decides.
    swapping = session.begin_nested() if swaps and scan.dry_run else None
    if swaps:
        rekey(session, scan.library_id, swaps)
        rows = session.execute(text(_POSTED), params).mappings().all()
    previous = {h.rel_path: h.previous_hash for h in posted}
    decided: list[Decided] = []
    stats: list[dict] = []
    matching: list[Decided] = []
    adding: list[Decided] = []  # removed by a person, added anyway (force_add)
    for r in rows:
        if r["decision"] in ("moved", "move_candidate", "move_skipped", "restored") or r["action"] == "skipped":
            decided.append(Decided(r["rel_path"], r["content_hash"], r["decision"] or "skipped", r["decided_id"],
                                   r["moved_from"]))
            continue
        d = Decided(r["rel_path"], r["content_hash"], "upload")
        if r["asset_id"] is not None:
            d.asset_id = r["asset_id"]
            if not (scan.force or r["preview_missing"] or r["asset_hash"] != r["content_hash"]):
                d.decision = "unchanged"
                if r["stat_differs"]:
                    stats.append({"asset_id": r["asset_id"], "size": r["file_size"], "mtime": r["file_mtime"]})
        elif r["removed"] and scan.force_add:
            adding.append(d)
            if r["removed_clip"] is not None:
                d.decision, d.asset_id = "restored", r["removed_clip"]
                stats.append({"asset_id": d.asset_id, "size": r["file_size"], "mtime": r["file_mtime"]})
        elif r["removed"]:
            d.decision = "skipped"
        elif moves_possible(scan, follow_moves):
            matching.append(d)
        decided.append(d)
    _match_moves(session, scan, matching)
    if swapping is not None:
        swapping.rollback()
    for d in decided:
        d.previous_hash = previous.get(d.rel_path)
    if not scan.dry_run:
        _add_anyway(session, scan.library_id, adding)
        moving = [d for d in matching if d.decision == "moved"]
        _not_moved(moving, apply_moves(session, scan.library_id,
                                       [(d.asset_id, d.moved_from, d.rel_path) for d in moving]))
        record_stats(session, stats)
    _record(session, scan.scan_id, decided)
    return decided


def _swaps(rows, previous: dict[str, str]) -> list[tuple[str, str, int]]:
    """The hashes made before fp1 that the fp1s sent take the place of, as
    (whole file's "sha256", fp1, size): the whole file's hash sent beside its
    fp1 (the plan asked, legacy), and an unchanged clip's own hash (the plan
    asked for its fp1, rekey: as a scan takes a clip whose size and modified
    time are as they were to be unchanged, it takes its content to be)."""
    swaps: list[tuple[str, str, int]] = []
    for r in rows:
        if not sampled(r["content_hash"]):
            continue
        if r["rel_path"] in previous:
            swaps.append((previous[r["rel_path"]], r["content_hash"], r["file_size"]))
        elif (r["reason"] == "rekey" and r["asset_id"] is not None and not r["stat_differs"]
              and whole_file(r["asset_hash"], r["file_size"])):
            swaps.append((r["asset_hash"], r["content_hash"], r["file_size"]))
    return swaps


def rekey(session: Session, library_id: str, swaps: list[tuple[str, str, int]]) -> None:
    """Hashes made before fp1 swapped for the fp1s of the same content (no
    commit): each (whole file's "sha256", fp1, file size) in the library's
    clips of that size, in any state, with their lineage's sources, and its
    files deleted for good. The fp1 is of the content the whole file's hash
    names, so every comparison comes out as it did; nothing is stale, and no
    clip's updated_at moves (a scan under way still takes it as it was)."""
    swaps = [s for s in swaps if whole_file(s[0], s[2]) and sampled(s[1])]
    if not swaps:
        return
    params = {"library_id": library_id, "olds": [s[0] for s in swaps], "news": [s[1] for s in swaps],
              "sizes": [s[2] for s in swaps]}
    session.execute(text("""
        WITH k AS (SELECT * FROM unnest(CAST(:olds AS text[]), CAST(:news AS text[]), CAST(:sizes AS bigint[]))
                   AS k(old, new, size)),
        swapped AS (
            UPDATE assets a SET content_hash = k.new FROM k
            WHERE a.library_id = :library_id AND a.content_hash = k.old AND a.file_size = k.size
            RETURNING a.asset_id, k.old, k.new
        )
        UPDATE artifact_lineage l SET source_hash = s.new FROM swapped s
        WHERE l.asset_id = s.asset_id AND l.source_hash = s.old
    """), params)
    # A path's file deleted for good is remembered once by its content.
    session.execute(text("""
        UPDATE ignored_files i SET content_hash = k.new
        FROM unnest(CAST(:olds AS text[]), CAST(:news AS text[]), CAST(:sizes AS bigint[])) AS k(old, new, size)
        WHERE i.library_id = :library_id AND i.content_hash = k.old AND i.file_size = k.size
          AND NOT EXISTS (SELECT 1 FROM ignored_files j
                          WHERE j.library_id = i.library_id AND j.rel_path = i.rel_path AND j.content_hash = k.new)
    """), params)


def _add_anyway(session: Session, library_id: str, files: list[Decided]) -> None:
    """Files a person removed that a scan adds anyway (force_add), no commit:
    each restored one's clip back in sight (the archive skipped too); one
    whose clip couldn't come back (another clip in sight took its path
    meanwhile) is an upload, which ingest makes right. And every one is
    forgotten as deleted for good, so ingest takes it and later scans don't
    skip it."""
    if not files:
        return
    restoring = [d for d in files if d.decision == "restored"]
    back = set(AssetRepository(session).bring_back_removed([d.asset_id for d in restoring]))
    for d in restoring:
        if d.asset_id not in back:
            d.decision, d.asset_id = "upload", None
    session.execute(text("""
        DELETE FROM ignored_files i
        USING unnest(CAST(:paths AS text[]), CAST(:hashes AS text[])) AS f(rel_path, content_hash)
        WHERE i.library_id = :library_id AND i.rel_path = f.rel_path
          AND (i.content_hash = f.content_hash OR i.content_hash IS NULL)
    """), {"library_id": library_id, "paths": [d.rel_path for d in files],
           "hashes": [d.content_hash for d in files]})



def _match_moves(session: Session, scan: ScanSession, files: list[Decided]) -> None:
    """Each file at a new path whose content is a gone clip's takes that clip:
    moved (moves "apply"), or a candidate (moves "ask"). The first file sent
    takes the first clip by id, as today's scanner took them in walk order
    from the listing in id order. The rest stay uploads."""
    if not files:
        return
    sources: dict[str, list[tuple[str, str]]] = {}
    for r in session.execute(text(f"""
        SELECT g.asset_id, g.rel_path, g.content_hash FROM ({GONE}) g
        WHERE g.content_hash = ANY(CAST(:hashes AS text[]))
        ORDER BY g.content_hash, g.asset_id COLLATE "C"
    """), {**_params(scan), "hashes": sorted({d.content_hash for d in files})}).mappings():
        sources.setdefault(r["content_hash"], []).append((r["asset_id"], r["rel_path"]))
    for d in files:
        if sources.get(d.content_hash):
            d.asset_id, d.moved_from = sources[d.content_hash].pop(0)
            d.decision = "moved" if scan.moves == "apply" else "move_candidate"


def _not_moved(moving: list[Decided], applied: set[str]) -> None:
    """A move that couldn't be made (the clip moved, changed or went
    meanwhile) is an upload: ingest then does what's right for the file."""
    for d in moving:
        if d.asset_id not in applied:
            d.decision, d.asset_id, d.moved_from = "upload", None, None


def apply_moves(session: Session, library_id: str, moves: list[tuple[str, str, str]]) -> set[str]:
    """Move clips to their new paths, one statement: (asset_id, from, to).
    Only a clip still in sight at its old path, to a path no clip in sight
    holds; each goes back in search (its document has the new path). Returns
    the clips moved."""
    if not moves:
        return set()
    try:
        # The paths taken are looked up once, by the library's paths index.
        return set(session.execute(text("""
            UPDATE assets a SET rel_path = m.rel_path, search_synced_at = NULL, updated_at = :now
            FROM unnest(CAST(:ids AS text[]), CAST(:froms AS text[]), CAST(:tos AS text[]))
                 AS m(asset_id, moved_from, rel_path)
            WHERE a.asset_id = m.asset_id AND a.library_id = :library_id AND a.deleted_at IS NULL
              AND a.rel_path = m.moved_from
              AND m.rel_path <> ALL (ARRAY(SELECT o.rel_path FROM assets o
                                           WHERE o.library_id = :library_id AND o.deleted_at IS NULL
                                             AND o.rel_path = ANY(CAST(:tos AS text[]))))
            RETURNING a.asset_id
        """), {"now": utcnow(), "library_id": library_id, "ids": [m[0] for m in moves],
               "froms": [m[1] for m in moves], "tos": [m[2] for m in moves]}).scalars())
    except IntegrityError as exc:  # another ingest put a clip at one of the paths just now
        raise PathTakenError() from exc


def record_stats(session: Session, stats: list[dict]) -> None:
    """Unchanged files' new size and time (touched, copied back), one
    statement, the clip online: otherwise every later scan reads the whole
    file again."""
    if stats:
        session.execute(text("""
            UPDATE assets a SET file_size = s.size, file_mtime = s.mtime, availability = 'online'
            FROM unnest(CAST(:ids AS text[]), CAST(:sizes AS bigint[]), CAST(:mtimes AS timestamptz[]))
                 AS s(asset_id, size, mtime)
            WHERE a.asset_id = s.asset_id AND a.deleted_at IS NULL
        """), {"ids": [s["asset_id"] for s in stats], "sizes": [s["size"] for s in stats],
               "mtimes": [s["mtime"] for s in stats]})


def _record(session: Session, scan_id: str, decided: list[Decided]) -> None:
    if decided:
        session.execute(text("""
            UPDATE scan_found f SET content_hash = d.content_hash, decision = d.decision, asset_id = d.asset_id,
                                    moved_from = d.moved_from, previous_hash = d.previous_hash
            FROM unnest(CAST(:paths AS text[]), CAST(:hashes AS text[]), CAST(:decisions AS text[]),
                        CAST(:ids AS text[]), CAST(:froms AS text[]), CAST(:previous AS text[]))
                 AS d(rel_path, content_hash, decision, asset_id, moved_from, previous_hash)
            WHERE f.scan_id = :scan_id AND f.rel_path = d.rel_path
        """), {"scan_id": scan_id, "paths": [d.rel_path for d in decided],
               "hashes": [d.content_hash for d in decided],
               "decisions": [d.decision for d in decided], "ids": [d.asset_id for d in decided],
               "froms": [d.moved_from for d in decided], "previous": [d.previous_hash for d in decided]})


def move_candidates(session: Session, scan_id: str, limit: int | None = None) -> list[dict]:
    """Moves waiting for a person's say (moves "ask"), by new path."""
    return [dict(r) for r in session.execute(text(
        "SELECT rel_path, asset_id, moved_from FROM scan_found"
        " WHERE scan_id = :scan_id AND decision = 'move_candidate' ORDER BY rel_path LIMIT :limit"
    ), {"scan_id": scan_id, "limit": limit}).mappings()]


def settle_moves(session: Session, scan: ScanSession, apply: bool) -> tuple[int, int, list[str]]:
    """The person's say on the scan's move candidates (no commit): applied
    (apply_moves), or skipped, when the moved files take no part: not
    uploaded, and their clips not archived (today's prompt, "Skip moves").
    Returns (moved, skipped, the new paths to upload: moves that couldn't be
    made). A dry run records the answer and moves nothing."""
    waiting = move_candidates(session, scan.scan_id)
    if not waiting:
        return 0, 0, []
    if not apply:
        _settle(session, scan.scan_id, [w["rel_path"] for w in waiting], "move_skipped")
        return 0, len(waiting), []
    if scan.dry_run:
        applied = waiting
    else:
        moved = apply_moves(session, scan.library_id,
                            [(w["asset_id"], w["moved_from"], w["rel_path"]) for w in waiting])
        applied = [w for w in waiting if w["asset_id"] in moved]
    _settle(session, scan.scan_id, [w["rel_path"] for w in applied], "moved")
    upload = [w["rel_path"] for w in waiting if w not in applied]
    if upload:
        session.execute(text(
            "UPDATE scan_found SET decision = 'upload', asset_id = NULL, moved_from = NULL"
            " WHERE scan_id = :scan_id AND rel_path = ANY(CAST(:paths AS text[]))"
        ), {"scan_id": scan.scan_id, "paths": upload})
    return len(applied), 0, upload


def _settle(session: Session, scan_id: str, paths: list[str], decision: str) -> None:
    session.execute(text(
        "UPDATE scan_found SET decision = :decision WHERE scan_id = :scan_id AND rel_path = ANY(CAST(:paths AS text[]))"
    ), {"scan_id": scan_id, "paths": paths, "decision": decision})


def gone_ids(session: Session, scan: ScanSession) -> list[str]:
    """The clips gone from this scan, by id: what its finish archives as missing."""
    return list(session.execute(text(f"SELECT g.asset_id FROM ({GONE}) g ORDER BY g.asset_id COLLATE \"C\""),
                                _params(scan)).scalars())


def gone_page(session: Session, scan: ScanSession, after: str | None, limit: int) -> list[dict]:
    """A page of the clips gone from this scan, by rel_path, with each one's
    media type: what its finish archives as missing unless that's held back.
    Moves decided by then (made, candidates, skipped) aren't gone."""
    return [dict(r) for r in session.execute(text(f"""
        SELECT g.asset_id, g.rel_path, g.media_type FROM ({GONE}) g
        WHERE CAST(:after AS text) IS NULL OR g.rel_path > :after
        ORDER BY g.rel_path LIMIT :limit
    """), {**_params(scan), "after": after, "limit": limit}).mappings()]


def skipped_page(session: Session, scan: ScanSession, after: str | None, limit: int) -> list[dict]:
    """A page of the files the scan skips because a person removed them, by
    rel_path: how each was removed (REMOVED_AS) and the clip removed (none
    when deleted for good). The removal named is one at the file's path that
    is this file, as the plan and the hashes decided: its content (once
    hashed), its size and time, or one whose content isn't known; of several,
    the first in REMOVED_AS, then one by content. A file whose removal was
    undone since (its clip restored) isn't listed: it's no longer removed.
    Its content is either hash sent (a dry run swaps no hash made before fp1)."""
    return [dict(r) for r in session.execute(text(f"""
        SELECT f.rel_path, m.removed_as, m.asset_id
        FROM scan_found f
        CROSS JOIN LATERAL (
            SELECT r.removed_as, r.asset_id FROM ({REMOVED}) r
            WHERE r.rel_path = f.rel_path
              AND (r.content_hash IS NULL OR r.content_hash = f.content_hash OR r.content_hash = f.previous_hash
                   OR (r.file_size = f.file_size AND r.file_mtime = f.file_mtime))
            ORDER BY array_position(CAST(:removed_as AS text[]), r.removed_as),
                     COALESCE(r.content_hash IN (f.content_hash, f.previous_hash), false) DESC,
                     r.asset_id COLLATE "C"
            LIMIT 1
        ) m
        WHERE f.scan_id = :scan_id AND COALESCE(f.decision, f.action) = 'skipped'
          AND (CAST(:after AS text) IS NULL OR f.rel_path > :after)
        ORDER BY f.rel_path LIMIT :limit
    """), {"scan_id": scan.scan_id, "library_id": scan.library_id, "removed_as": list(REMOVED_AS),
           "after": after, "limit": limit}).mappings()]


def totals(session: Session, scan: ScanSession) -> dict[str, int]:
    """What the scan decided, for its finish: files found of its media type,
    moved (in a dry run, its candidates too), unchanged and skipped."""
    return dict(session.execute(text("""
        SELECT count(*) FILTER (WHERE CAST(:media_type AS text) = 'all' OR media_type = CAST(:media_type AS text))
                   AS of_media_type,
               count(*) FILTER (WHERE decision IN ('moved', 'move_candidate')) AS moved,
               count(*) FILTER (WHERE COALESCE(decision, action) = 'unchanged') AS unchanged,
               count(*) FILTER (WHERE COALESCE(decision, action) = 'skipped') AS skipped
        FROM scan_found WHERE scan_id = :scan_id
    """), {"scan_id": scan.scan_id, "media_type": scan.media_type}).mappings().one())


def with_newer_copies(session: Session, asset_ids: list[str]) -> list[str]:
    """Of these, in order, those with a newer clip in sight of the same
    content in their library: the only ones a copy-then-delete handover can
    move (AssetRepository.newer_copy_candidates), found in one statement."""
    if not asset_ids:
        return []
    found = set(session.execute(text("""
        SELECT o.asset_id FROM assets o
        WHERE o.asset_id = ANY(CAST(:ids AS text[])) AND o.content_hash IS NOT NULL
          AND EXISTS (SELECT 1 FROM assets c WHERE c.library_id = o.library_id AND c.content_hash = o.content_hash
                      AND c.deleted_at IS NULL AND c.asset_id <> o.asset_id AND c.created_at > o.created_at)
    """), {"ids": asset_ids}).scalars())
    return [a for a in asset_ids if a in found]


def close(session: Session, scan: ScanSession) -> None:
    """Finished: the listing goes; the scan stays until it expires, so a
    finish sent again gets the answer it kept (no commit)."""
    session.execute(text("DELETE FROM scan_found WHERE scan_id = :s"), {"s": scan.scan_id})
    now = utcnow()
    scan.finished_at = now
    scan.expires_at = now + TTL
    session.add(scan)
