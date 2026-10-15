"""Search sync: keep each clip's searchable text and each transcript's segments in step (ADR-019).

What's searched is in the account's database, beside what it's made from
(src/server/search/text_search.py). A scene's words are computed with its own
row, so two things are kept here, each marked on its clip when synced:

- a clip's row in asset_search (search_synced_at): its description and tags
  as a person sees them, the text read in it, its note, its transcript and
  its path. Written when one of them is saved (try_sync_asset), and by the
  sweep for any still stale (STALE_SEARCH);
- a transcript's segments (transcript_synced_at), written whenever it's
  saved, removed or comes back (index_transcript_segments), and by the sweep.

Each is written and marked in one transaction: what search finds is never
ahead of or behind what's marked, only (for a moment) behind a save.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from sqlalchemy import text
from sqlmodel import Session

from src.server.models.tenant import Asset, AssetMetadata
from src.shared.utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Everything again (POST /v1/upkeep/search-sync force=true)
# ---------------------------------------------------------------------------

REINDEX_RESETS = {
    "assets": "UPDATE assets SET search_synced_at = NULL",
    "transcripts": "UPDATE assets SET transcript_synced_at = NULL",
}


# ---------------------------------------------------------------------------
# What's searched
# ---------------------------------------------------------------------------

def _path_to_tokens(rel_path: str) -> str:
    """A path's words: its folders' and file's names split at / \\ _ - and ."""
    s = re.sub(r"[/\\_\-.]", " ", rel_path)
    return re.sub(r" +", " ", s).strip()


def search_row(asset: Asset, meta: AssetMetadata | None, ocr_text: str = "",
               corrections: dict | None = None) -> dict:
    """A clip's row in asset_search, as a person sees it: its latest AI
    description and tags and the text read in its picture, or what a person
    wrote instead; its note, transcript and path. Its snippet (what a hit
    shows) is the description, else its first tags."""
    from src.server.repository.corrections import apply, machine_tags

    data = (meta.data if meta else None) or {}
    description, tags, ocr_text, _ = apply(corrections, data.get("description", ""), machine_tags(data), ocr_text)
    description = description or ""
    return {
        "asset_id": asset.asset_id,
        "described": " ".join([description, *tags]).strip(),
        "note": asset.note or "",
        "ocr_text": ocr_text or "",
        "transcript_text": asset.transcript_text or "",
        "path_words": _path_to_tokens(asset.rel_path),
        "snippet": description or " · ".join(tags[:4]) or None,
    }


def put_search_rows(session: Session, rows: list[dict]) -> None:
    """Write clips' rows, each replacing the one it had (no commit)."""
    if not rows:
        return
    session.execute(text("""
        INSERT INTO asset_search (asset_id, described, note, ocr_text, transcript_text, path_words, snippet)
        SELECT * FROM unnest(CAST(:ids AS text[]), CAST(:described AS text[]), CAST(:notes AS text[]),
                             CAST(:ocr AS text[]), CAST(:transcripts AS text[]), CAST(:paths AS text[]),
                             CAST(:snippets AS text[]))
        ON CONFLICT (asset_id) DO UPDATE SET
            described = EXCLUDED.described, note = EXCLUDED.note, ocr_text = EXCLUDED.ocr_text,
            transcript_text = EXCLUDED.transcript_text, path_words = EXCLUDED.path_words, snippet = EXCLUDED.snippet
    """), {"ids": [r["asset_id"] for r in rows], "described": [r["described"] for r in rows],
           "notes": [r["note"] for r in rows], "ocr": [r["ocr_text"] for r in rows],
           "transcripts": [r["transcript_text"] for r in rows], "paths": [r["path_words"] for r in rows],
           "snippets": [r["snippet"] for r in rows]})


def segment_rows(asset: Asset, srt: str | None) -> list[dict]:
    """A transcript's segments, in order: each one's times and text."""
    from src.server.srt import parse_srt_segments

    return [{"seq": i, "start_ms": seg.start_ms, "end_ms": seg.end_ms, "text": seg.text}
            for i, seg in enumerate(parse_srt_segments(srt) if srt else [])]


def _put_segments(session: Session, asset_ids: list[str], rows: dict[str, list[dict]]) -> None:
    """Replace these clips' transcript segments with rows (by clip; none: they go). No commit."""
    session.execute(text("DELETE FROM transcript_segments WHERE asset_id = ANY(CAST(:ids AS text[]))"),
                    {"ids": asset_ids})
    flat = [(asset_id, r) for asset_id in asset_ids for r in rows.get(asset_id, [])]
    if flat:
        session.execute(text("""
            INSERT INTO transcript_segments (asset_id, seq, start_ms, end_ms, text)
            SELECT * FROM unnest(CAST(:ids AS text[]), CAST(:seqs AS int[]), CAST(:starts AS int[]),
                                 CAST(:ends AS int[]), CAST(:texts AS text[]))
        """), {"ids": [a for a, _ in flat], "seqs": [r["seq"] for _, r in flat],
               "starts": [r["start_ms"] for _, r in flat], "ends": [r["end_ms"] for _, r in flat],
               "texts": [r["text"] for _, r in flat]})


# ---------------------------------------------------------------------------
# When something changes (best effort: the sweep catches what these miss)
# ---------------------------------------------------------------------------

def index_transcript_segments(session: Session, asset: Asset) -> None:
    """Replace a clip's transcript segments with its transcript as
    committed (none: they just go).

    Called wherever a transcript changes: when one is submitted or removed
    (a clip out of sight keeps its segments; search leaves it out). One at a time
    per clip (an advisory lock), each reading the transcript once it holds
    the clip: of two saves at once, the later one's segments are what search
    finds. In its own transaction on the session's database (the caller's
    isn't touched), with the clip's transcript_synced_at; on failure that's
    left as it was and the sweep does it."""
    try:
        with Session(session.get_bind()) as own:
            own.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                        {"k": f"transcript_index:{asset.asset_id}"})
            started = utcnow()
            now = own.get(Asset, asset.asset_id)
            rows = segment_rows(now, now.transcript_srt) if now is not None else []
            _put_segments(own, [asset.asset_id], {asset.asset_id: rows})
            # Never waits long on a lock the caller holds on the row.
            own.execute(text("SET LOCAL lock_timeout = '2s'"))
            own.execute(text("UPDATE assets SET transcript_synced_at = :t WHERE asset_id = :a"),
                        {"t": started, "a": asset.asset_id})
            own.commit()
    except Exception as exc:
        logger.warning("Transcript segments of %s not written: %s", asset.asset_id, exc)


def try_sync_asset(session: Session, asset: Asset, meta: AssetMetadata | None = None) -> bool:
    """Write a clip's search row now, and mark it synced. Returns True on
    success; on failure, logs a warning and never raises (the sweep does it
    later). Commits."""
    from src.server.repository.corrections import CorrectionsRepository
    from src.server.repository.tenant import AssetOcrRepository

    try:
        row = search_row(asset, meta, AssetOcrRepository(session).text_for(asset.asset_id),
                         CorrectionsRepository(session).get(asset.asset_id))
        with session.begin_nested():  # a failure undoes this write only, never the caller's
            put_search_rows(session, [row])
    except Exception as exc:
        logger.warning("Search row of %s not written: %s", asset.asset_id, exc)
        return False
    asset.search_synced_at = utcnow()
    session.add(asset)
    session.commit()
    return True


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

# A clip's row is stale until synced after the latest of its description,
# its OCR and its corrections (a note, a move or a transcript clears its sync
# time when saved). SQL on active_assets a; the repair summary counts with the same rule.
STALE_SEARCH = (
    "(a.search_synced_at IS NULL"
    " OR a.search_synced_at < (SELECT MAX(sm.generated_at) FROM asset_metadata sm WHERE sm.asset_id = a.asset_id)"
    " OR a.search_synced_at < (SELECT so.generated_at FROM asset_ocr so WHERE so.asset_id = a.asset_id)"
    " OR a.search_synced_at < (SELECT sc.updated_at FROM asset_corrections sc WHERE sc.asset_id = a.asset_id))"
)
# Clips and transcripts a batch, and how long one sweep goes on for (an
# account's): a whole library's rows are written again in a few sweeps.
BATCH = 1000
SWEEP_SEC = 30.0


def _mark_synced(session: Session, column: str, at: datetime, ids: list[str], xmins: list[str]) -> int:
    """Mark these clips synced at `at`, each only if its xmin is still the
    one read (nothing wrote the clip since its row was built). Returns how many were."""
    return session.execute(
        text(f"UPDATE assets t SET {column} = :at"
             " FROM unnest(CAST(:ids AS text[]), CAST(:xmins AS text[])) AS v(id, x)"
             " WHERE t.asset_id = v.id AND t.xmin::text = v.x"),
        {"at": at, "ids": ids, "xmins": xmins},
    ).rowcount


def _write_clips(session: Session, which: str, params: dict) -> tuple[list, int]:
    """The rows of the clips in sight `which` picks (SQL on active_assets a),
    in id order, written and marked synced together. Returns the clips
    looked at and how many were marked (one written as something wrote it
    isn't: the sweep writes it again)."""
    from src.server.repository.tenant import AssetMetadataRepository

    # Synced as of when this began: a description, OCR, correction or note
    # saved meanwhile is newer, and goes in next time. And only a clip
    # nothing wrote meanwhile (its xmin) is marked: a write that cleared its
    # sync time (a note, a move; every description and OCR saved, whose
    # time is taken before it commits: repository/tenant.py _unsynced) stays cleared.
    started = utcnow()
    batch = session.execute(text(f"""
        SELECT a.asset_id, COALESCE(o.text, '') AS ocr_text,
               c.description AS c_description, c.ocr_text AS c_ocr_text,
               c.asset_id AS c_asset_id, c.tags AS c_tags, x.xmin::text AS row_xmin
        FROM active_assets a
        JOIN assets x ON x.asset_id = a.asset_id
        LEFT JOIN asset_ocr o ON o.asset_id = a.asset_id
        LEFT JOIN asset_corrections c ON c.asset_id = a.asset_id
        WHERE {which}
        ORDER BY a.asset_id
        LIMIT {BATCH}
    """), params).fetchall()
    meta_repo = AssetMetadataRepository(session)
    rows: list[dict] = []
    ids: list[str] = []
    xmins: list[str] = []
    for r in batch:
        asset = session.get(Asset, r.asset_id)
        if asset is None:
            continue
        corrections = None
        if r.c_asset_id is not None:  # a person wrote something for the clip
            corrections = {"description": r.c_description, "ocr_text": r.c_ocr_text, "tags": r.c_tags}
        rows.append(search_row(asset, meta_repo.get_latest(asset_id=r.asset_id), r.ocr_text, corrections))
        ids.append(r.asset_id)
        xmins.append(r.row_xmin)
    put_search_rows(session, rows)
    marked = _mark_synced(session, "search_synced_at", started, ids, xmins)
    session.commit()
    return batch, marked


def sync_clips(session: Session, asset_ids: list[str]) -> int:
    """Write these clips' rows now (those in sight), a batch at a time, and
    mark them synced: what a request that saved many at once (descriptions,
    OCR) does after its commit. Best effort, as try_sync_asset: a batch that
    fails is logged and left to the sweep. Returns how many were marked. Commits."""
    marked = 0
    for i in range(0, len(asset_ids), BATCH):
        try:
            _, n = _write_clips(session, "a.asset_id = ANY(CAST(:ids AS text[]))", {"ids": asset_ids[i:i + BATCH]})
        except Exception as exc:  # noqa: BLE001 — the sweep writes them
            session.rollback()
            logger.warning("Search rows of %d clips not written: %s", len(asset_ids[i:i + BATCH]), exc)
            continue
        marked += n
    return marked


def _sync_clips(session: Session, after: str) -> tuple[str | None, int]:
    """The next batch of stale clips after `after` (by id), written. Returns
    the last id looked at (None when there were no more) and how many were marked."""
    batch, marked = _write_clips(session, f"{STALE_SEARCH} AND a.asset_id > :after", {"after": after})
    return (batch[-1].asset_id if len(batch) == BATCH else None), marked


def _sync_transcripts(session: Session, after: str) -> tuple[str | None, int]:
    """The next batch of stale transcripts after `after` (by clip id): their
    segments, written and marked synced together. Returns as _sync_clips."""
    started = utcnow()
    batch = session.execute(text(f"""
        SELECT a.asset_id, x.xmin::text AS row_xmin FROM active_assets a JOIN assets x ON x.asset_id = a.asset_id
        WHERE a.transcript_srt IS NOT NULL
          AND (a.transcript_synced_at IS NULL OR a.transcript_synced_at < a.transcribed_at)
          AND a.asset_id > :after
        ORDER BY a.asset_id
        LIMIT {BATCH}
    """), {"after": after}).fetchall()
    rows: dict[str, list[dict]] = {}
    for r in batch:
        asset = session.get(Asset, r.asset_id)
        if asset is not None:
            rows[r.asset_id] = segment_rows(asset, asset.transcript_srt)
    ids = [r.asset_id for r in batch if r.asset_id in rows]
    _put_segments(session, ids, rows)
    marked = _mark_synced(session, "transcript_synced_at", started, ids,
                          [r.row_xmin for r in batch if r.asset_id in rows])
    session.commit()
    return (batch[-1].asset_id if len(batch) == BATCH else None), marked


def run_search_sync_sweep(session: Session) -> dict:
    """Write the rows of every stale clip and the segments of every stale
    transcript, a batch at a time by id for up to SWEEP_SEC (what's left
    goes on next sweep; each is looked at once a sweep). Returns {"synced", "failed", "transcripts_synced", "transcripts_failed"}:
    failed counts batches that couldn't be written (rolled back, tried next
    sweep)."""
    result = {"synced": 0, "failed": 0, "transcripts_synced": 0, "transcripts_failed": 0}
    deadline = time.monotonic() + SWEEP_SEC
    for sync, done, failed in ((_sync_clips, "synced", "failed"),
                               (_sync_transcripts, "transcripts_synced", "transcripts_failed")):
        after: str | None = ""
        while after is not None and time.monotonic() < deadline:
            try:
                after, n = sync(session, after)
            except Exception as exc:  # noqa: BLE001 — a query that failed: tried again next sweep
                session.rollback()
                logger.warning("Search sync batch (%s) failed: %s", done, exc)
                result[failed] += 1
                break
            result[done] += n
    return result
