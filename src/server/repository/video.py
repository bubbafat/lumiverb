"""A video's scenes and the chunks it's indexed in. Every repository takes a Session."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, insert, text
from sqlmodel import Session, select

from src.shared.utils import utcnow
from src.server.models.tenant import (
    VideoIndexChunk,
    VideoScene,
)
from ulid import ULID


FAILURE_BLOCK_THRESHOLD = 3

class VideoSceneRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_scenes_for_asset(self, asset_id: str, *, describing: bool = False) -> list[VideoScene]:
        """The scenes a video shows, ordered by start_ms. describing: the ones
        to describe next, which are a redo's found scenes while it has them
        (they're swapped in once described), else the ones shown."""
        staged = None
        if describing and self._session.execute(text(
                "SELECT 1 FROM video_scenes WHERE asset_id = :a AND staged = 'found' LIMIT 1"), {"a": asset_id}).first():
            staged = "found"
        where = VideoScene.staged.is_(None) if staged is None else VideoScene.staged == staged  # type: ignore[union-attr]
        return list(
            self._session.exec(
                select(VideoScene)
                .where(VideoScene.asset_id == asset_id, where)
                .order_by(VideoScene.start_ms)
            ).all()
        )

    def get_by_id(self, scene_id: str) -> VideoScene | None:
        return self._session.get(VideoScene, scene_id)

    def files_of(self, asset_ids: list[str]) -> list[tuple[str, int, str | None, str | None]]:
        """Every scene of these clips: (asset_id, rep_frame_ms, proxy_key, thumbnail_key)."""
        return [tuple(r) for r in self._session.execute(text(
            "SELECT asset_id, rep_frame_ms, proxy_key, thumbnail_key FROM video_scenes WHERE asset_id = ANY(:ids)"),
            {"ids": asset_ids}).all()]

    def update_vision(
        self,
        scene_id: str,
        model_id: str,
        model_version: str,
        description: str,
        tags: list[str],
        lineage: dict | None = None,
    ) -> None:
        """Write vision results back to a scene row, with how they were made."""
        scene = self._session.get(VideoScene, scene_id)
        if scene is None:
            raise ValueError(f"Scene not found: {scene_id}")
        scene.description = description
        scene.tags = tags
        scene.lineage = lineage  # searchable once committed: its words are computed with the row
        self._session.add(scene)
        self._session.commit()

    # -- A redo: scenes found beside the ones the clip shows ---------------------------------

    def shows_scenes(self, asset_id: str) -> bool:
        """Whether the clip's scenes are found: a run now finds new ones beside them."""
        return bool(self._session.execute(text("SELECT video_indexed FROM assets WHERE asset_id = :a"),
                                          {"a": asset_id}).scalar())

    def found_beside(self, asset_id: str, made: dict) -> tuple[dict | None, int | None]:
        """A redo's run, found beside the scenes the clip shows (which stay, with
        their descriptions, images and search entries, until the new ones are
        found and described: swap_in). Its own run under way goes on. Returns
        the run its chunks record, or None when there's nothing to find beside
        (the clip shows no scenes yet, its first run going on, or shows them
        found this way already); and how many scenes another run had staged,
        dropped with its chunks (None when no chunks were dropped). Commits."""
        row = self._session.execute(text(
            "SELECT a.video_indexed, l.producer, l.producer_version, l.settings_hash"
            " FROM assets a LEFT JOIN artifact_lineage l ON l.asset_id = a.asset_id AND l.artifact = 'scenes'"
            " WHERE a.asset_id = :a FOR UPDATE OF a"), {"a": asset_id}).first()
        if row is None or not row.video_indexed or (row.producer, row.producer_version, row.settings_hash) == (
                made["producer"], str(made.get("version") or ""), made.get("settings_hash")):
            self._session.commit()
            return None, None
        run = self._session.execute(text("SELECT run FROM video_index_chunks WHERE asset_id = :a LIMIT 1"),
                                    {"a": asset_id}).scalar()
        if same_run(run, made):
            self._session.commit()
            return made, None
        # The chunks the scenes shown were found with, or another run's, go, with
        # the scenes that run staged; the scenes shown stay.
        dropped = self._session.execute(text("DELETE FROM video_scenes WHERE asset_id = :a AND staged IS NOT NULL"),
                                        {"a": asset_id}).rowcount
        self._session.execute(text("DELETE FROM video_index_chunks WHERE asset_id = :a"), {"a": asset_id})
        self._session.commit()
        return made, dropped

    def mark_found(self, asset_id: str) -> None:
        """A redo's scenes, every chunk in: found, described next (not committed)."""
        self._session.execute(text("UPDATE video_scenes SET staged = 'found' WHERE asset_id = :a AND staged = 'finding'"),
                              {"a": asset_id})

    def swap_in(self, asset_id: str, described: dict | None) -> Swap | None:
        """A redo's found scenes become the ones the clip shows, in one step: the
        old ones go (and with them what search found of them). described: how
        the new ones were described (recorded for the clip), or None when
        nothing describes them. None when there's nothing to swap in (done
        already). Commits; the old scenes' images are the caller's to delete."""
        from src.server.repository import lineage

        self._session.execute(text("SELECT 1 FROM assets WHERE asset_id = :a FOR UPDATE"), {"a": asset_id})
        new = self._session.execute(text("SELECT scene_id, rep_frame_ms FROM video_scenes"
                                         " WHERE asset_id = :a AND staged = 'found'"), {"a": asset_id}).all()
        if not new:
            self._session.rollback()
            return None
        old = self._session.execute(text("SELECT scene_id, rep_frame_ms FROM video_scenes"
                                         " WHERE asset_id = :a AND staged IS NULL"), {"a": asset_id}).all()
        self._session.execute(text("DELETE FROM video_scenes WHERE asset_id = :a AND staged IS NULL"), {"a": asset_id})
        self._session.execute(text("UPDATE video_scenes SET staged = NULL WHERE asset_id = :a AND staged = 'found'"),
                              {"a": asset_id})
        if described is not None:
            lineage.record(self._session, asset_id, "scene_vision", described, commit=False)
        library_id = self._session.execute(text("SELECT library_id FROM assets WHERE asset_id = :a"),
                                           {"a": asset_id}).scalar()
        self._session.commit()
        return Swap(library_id=library_id, new_frames={r.rep_frame_ms for r in new},
                    old_frames={r.rep_frame_ms for r in old}, new=len(new), old=len(old))

    def run_of(self, scene_id: str, scene_lineage: dict) -> SceneRun | None:
        """The scene's clip, which of its runs the scene is in (staged), and how
        many of that run's scenes aren't described as scene_lineage says yet."""
        row = self._session.execute(text("SELECT asset_id, staged FROM video_scenes WHERE scene_id = :s"),
                                    {"s": scene_id}).first()
        if row is None:
            return None
        undescribed = self._session.execute(text(
            "SELECT count(*) FROM video_scenes WHERE asset_id = :a"
            " AND staged IS NOT DISTINCT FROM CAST(:st AS text)"
            " AND (description IS NULL OR lineage IS DISTINCT FROM CAST(:l AS jsonb))"
        ), {"a": row.asset_id, "st": row.staged, "l": json.dumps(scene_lineage)}).scalar()
        return SceneRun(asset_id=row.asset_id, staged=row.staged, undescribed=undescribed or 0)


def same_run(run: dict | None, made: dict) -> bool:
    """Whether a run's chunks find scenes as made says (its level too)."""
    keys = ("producer", "version", "settings_hash", "level")
    return run is not None and all(str(run.get(k) or "") == str(made.get(k) or "") for k in keys)


@dataclass(frozen=True)
class Swap:
    """A redo's scenes swapped in: the clip's library, the frames of the new
    scenes' and the old ones' images, and how many of each."""
    library_id: str
    new_frames: set[int]
    old_frames: set[int]
    new: int
    old: int


@dataclass(frozen=True)
class SceneRun:
    asset_id: str
    staged: str | None  # None: the scenes the clip shows; "finding"/"found": a redo's
    undescribed: int


class ChunksBusy(Exception):
    """A clip's chunk is claimed (its lease not run out) or failed in this
    run: no other is handed out, so scenes are found, and numbered, in order.
    status: the busy chunk's, "claimed" or "failed"."""

    def __init__(self, message: str, status: str) -> None:
        super().__init__(message)
        self.status = status


class VideoIndexChunkRepository:
    """Repository for video_index_chunks and chunk completion (scenes)."""

    LEASE_MINUTES: int = 5

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, chunk_id: str) -> VideoIndexChunk | None:
        return self._session.exec(select(VideoIndexChunk).where(VideoIndexChunk.chunk_id == chunk_id)).first()

    def create_chunks_for_asset(
        self,
        asset_id: str,
        duration_sec: float,
        scene_sec: float,
        run: dict | None = None,
    ) -> int:
        """
        Pre-generate all pending chunks for a video asset: whole scene windows
        of about scene_sec each (src/producers/scenes/detail.py chunk_grid),
        so no chunk ends inside a scene. run: how they find its scenes (a redo's).
        Idempotent — skips creation if chunks already exist for this asset.
        Returns count of chunks created.
        """
        from src.producers.scenes.detail import chunk_grid

        existing = self._session.exec(
            select(VideoIndexChunk).where(VideoIndexChunk.asset_id == asset_id)
        ).first()
        if existing:
            return 0

        now = utcnow()
        chunks = [{
            "chunk_id": "chk_" + str(ULID()),
            "asset_id": asset_id,
            "chunk_index": index,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "status": "pending",
            "run": run,
            "created_at": now,
        } for index, (start_ms, end_ms) in enumerate(chunk_grid(duration_sec, scene_sec))]

        if chunks:
            self._session.execute(insert(VideoIndexChunk), chunks)
            self._session.commit()
        return len(chunks)

    def retry_failed(self, asset_id: str) -> int:
        """A new run over the clip's chunks: those that failed in an earlier
        run are pending again. (Within a run a failed chunk stays failed:
        claim_next_chunk never hands it out again.) Returns how many."""
        failed = self._session.exec(
            select(VideoIndexChunk).where(
                VideoIndexChunk.asset_id == asset_id,
                VideoIndexChunk.status == "failed",
            )
        ).all()
        for chunk in failed:
            chunk.status = "pending"
            chunk.worker_id = None
            chunk.claimed_at = None
            chunk.lease_expires_at = None
            self._session.add(chunk)
        if failed:
            self._session.commit()
        return len(failed)

    def claim_next_chunk(
        self,
        asset_id: str,
        worker_id: str,
    ) -> VideoIndexChunk | None:
        """
        Claim the next pending chunk for an asset (lowest chunk_index), or
        one whose lease has expired. A failed chunk isn't handed out again:
        it waits for the next run (retry_failed), which the scheduler starts
        under its back-off. Returns None if no claimable chunks remain;
        raises ChunksBusy while one is claimed or failed (chunks go in order).
        """
        now = utcnow()
        lease_expires = now + timedelta(minutes=self.LEASE_MINUTES)

        reclaimable = self._session.exec(
            select(VideoIndexChunk).where(
                VideoIndexChunk.asset_id == asset_id,
                VideoIndexChunk.status == "claimed",
                VideoIndexChunk.lease_expires_at < now,
            )
        ).all()
        for chunk in reclaimable:
            chunk.status = "pending"
            chunk.worker_id = None
            chunk.claimed_at = None
            chunk.lease_expires_at = None
            self._session.add(chunk)
        if reclaimable:
            self._session.flush()

        busy = self._session.exec(
            select(VideoIndexChunk).where(
                VideoIndexChunk.asset_id == asset_id,
                VideoIndexChunk.status.in_(("claimed", "failed")),  # type: ignore[attr-defined]
            )
        ).first()
        if busy is not None:
            self._session.commit()
            raise ChunksBusy(f"chunk {busy.chunk_index} is {busy.status}", busy.status)

        chunk = self._session.exec(
            select(VideoIndexChunk)
            .where(
                VideoIndexChunk.asset_id == asset_id,
                VideoIndexChunk.status == "pending",
            )
            .order_by(VideoIndexChunk.chunk_index)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()

        if chunk is None:
            return None

        chunk.status = "claimed"
        chunk.worker_id = worker_id
        chunk.claimed_at = now
        chunk.lease_expires_at = lease_expires
        self._session.add(chunk)
        self._session.commit()
        self._session.refresh(chunk)
        return chunk

    def complete_chunk(
        self,
        chunk_id: str,
        worker_id: str,
        scenes: list[dict],
        staged: str | None = None,
    ) -> bool:
        """
        Complete a chunk: persist its scenes and mark it complete. Flushed,
        not committed: the caller commits (with what the last chunk finishes).
        staged: "finding" for a redo's scenes, found beside the ones shown.
        Returns False if chunk not found or not owned by worker_id.
        """
        chunk = self._session.exec(
            select(VideoIndexChunk).where(VideoIndexChunk.chunk_id == chunk_id)
        ).first()
        if chunk is None or chunk.worker_id != worker_id or chunk.status != "claimed":
            return False

        now = utcnow()

        # The clip's scenes are numbered here, after those of the same set
        # already stored (chunks complete in order), so a resumed clip goes on
        # counting. The clip is locked meanwhile; (asset_id, staged,
        # scene_index) is unique.
        self._session.execute(text("SELECT 1 FROM assets WHERE asset_id = :a FOR UPDATE"), {"a": chunk.asset_id})
        first = int(self._session.execute(
            text("SELECT COALESCE(max(scene_index) + 1, 0) FROM video_scenes WHERE asset_id = :a"
                 " AND staged IS NOT DISTINCT FROM CAST(:st AS text)"),
            {"a": chunk.asset_id, "st": staged}).scalar() or 0)

        for i, s in enumerate(scenes):
            self._session.add(VideoScene(
                scene_id="scn_" + str(ULID()),
                asset_id=chunk.asset_id,
                scene_index=first + i,
                start_ms=s["start_ms"],
                end_ms=s["end_ms"],
                rep_frame_ms=s["rep_frame_ms"],
                rep_frame_sha256=s.get("rep_frame_sha256"),
                proxy_key=s.get("proxy_key"),
                thumbnail_key=s.get("thumbnail_key"),
                description=s.get("description"),
                tags=s.get("tags"),
                sharpness_score=s.get("sharpness_score"),
                keep_reason=s.get("keep_reason"),
                phash=s.get("phash"),
                staged=staged,
                created_at=now,
            ))

        chunk.status = "completed"
        chunk.completed_at = now
        self._session.add(chunk)
        self._session.flush()
        return True

    def merge_alike(self, asset_id: str, within: int, staged: str | None = None) -> tuple[int, int]:
        """The discard pass, once every chunk of the clip is found: one pass
        left to right (src/producers/scenes/detail.py merge_runs), each run of
        scenes alike kept as its first, which takes the run's whole span; the
        rest go, and the scenes are numbered again in order. Scenes hold no
        human data (and aren't described yet: the clip isn't found until
        this is done). staged: the set found ("finding" for a redo's; None,
        the ones shown, for a first run). Doesn't commit. Returns (found, kept)."""
        from src.producers.scenes.detail import merge_runs

        one_set = "asset_id = :a AND staged IS NOT DISTINCT FROM CAST(:st AS text)"
        rows = self._session.execute(text(
            f"SELECT scene_id, end_ms, phash FROM video_scenes WHERE {one_set} ORDER BY start_ms, scene_index"
        ), {"a": asset_id, "st": staged}).all()
        runs = merge_runs([r.phash for r in rows], within)
        gone = [rows[k].scene_id for first, last in runs for k in range(first + 1, last + 1)]
        if gone:
            self._session.execute(text("DELETE FROM video_scenes WHERE scene_id = ANY(:ids)"), {"ids": gone})
            for first, last in runs:
                if last > first:
                    self._session.execute(text("UPDATE video_scenes SET end_ms = :e WHERE scene_id = :s"),
                                          {"e": rows[last].end_ms, "s": rows[first].scene_id})
        kept = [rows[first].scene_id for first, _ in runs]
        # Numbered again, 0 up: through negatives, as (asset_id, staged, scene_index) is unique.
        self._session.execute(text(
            f"UPDATE video_scenes SET scene_index = -1 - scene_index WHERE {one_set}"), {"a": asset_id, "st": staged})
        self._session.execute(text(
            "UPDATE video_scenes v SET scene_index = k.n - 1"
            " FROM unnest(CAST(:ids AS text[])) WITH ORDINALITY AS k(scene_id, n) WHERE v.scene_id = k.scene_id"
        ), {"ids": kept})
        self._session.expire_all()
        return len(rows), len(kept)

    def fail_chunk(self, chunk_id: str, worker_id: str, error_message: str) -> bool:
        """Mark a chunk as failed."""
        chunk = self._session.exec(
            select(VideoIndexChunk).where(VideoIndexChunk.chunk_id == chunk_id)
        ).first()
        if chunk is None or chunk.worker_id != worker_id or chunk.status != "claimed":
            return False  # a completed chunk stays completed
        chunk.status = "failed"
        chunk.error_message = error_message
        self._session.add(chunk)
        self._session.commit()
        return True

    def all_chunks_complete(self, asset_id: str) -> bool:
        """True if every chunk for this asset is completed."""
        incomplete = self._session.exec(
            select(VideoIndexChunk).where(
                VideoIndexChunk.asset_id == asset_id,
                VideoIndexChunk.status != "completed",
            )
        ).first()
        return incomplete is None

    def chunk_count(self, asset_id: str) -> int:
        result = self._session.execute(
            select(func.count()).select_from(VideoIndexChunk).where(
                VideoIndexChunk.asset_id == asset_id
            )
        )
        return int(result.scalar() or 0)
