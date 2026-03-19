from __future__ import annotations

import logging
import re
from typing import Iterable

from sqlmodel import Session

from src.core.config import get_settings
from src.workers.output_events import emit_event
from src.core.io_utils import normalize_path_prefix
from src.core.utils import utcnow
from src.models.tenant import Asset, AssetMetadata, Library, VideoScene
from src.repository.tenant import (
    AssetMetadataRepository,
    AssetRepository,
    LibraryRepository,
    SearchSyncQueueRepository,
    VideoSceneRepository,
)
from src.search.quickwit_client import QuickwitClient

logger = logging.getLogger(__name__)


def _path_to_tokens(rel_path: str) -> str:
    """Turn rel_path into space-separated tokens for search (e.g. Photos/UK2024/DSC07171.ARW → Photos UK2024 DSC07171 ARW)."""
    s = re.sub(r"[/\\_\-.]", " ", rel_path)
    return re.sub(r" +", " ", s).strip()


class SearchSyncWorker:
    """
    Quickwit search sync worker.

    This worker does NOT use the generic BaseWorker/job queue. Instead it drains
    the search_sync_queue outbox table in batches:

    1. Claims pending rows with FOR UPDATE SKIP LOCKED.
    2. Fetches the corresponding asset and AI metadata.
    3. Builds Quickwit documents.
    4. Sends them to Quickwit ingest API.
    5. Marks rows as synced.
    """

    def __init__(
        self,
        session: Session,
        library_id: str,
        quickwit: QuickwitClient | None = None,
        batch_size: int = 100,
        path_prefix: str | None = None,
        output_mode: str = "human",
    ) -> None:
        self._session = session
        self._library_id = library_id
        self._batch_size = batch_size
        self._path_prefix = normalize_path_prefix(path_prefix)
        self._asset_repo = AssetRepository(session)
        self._meta_repo = AssetMetadataRepository(session)
        self._library_repo = LibraryRepository(session)
        self._queue_repo = SearchSyncQueueRepository(session)
        self._quickwit = quickwit or QuickwitClient()
        self._output_mode = output_mode
        self._lease_minutes = get_settings().search_sync_lease_minutes

    def pending_count(self) -> int:
        """Return number of rows claim_batch() will process (pending + expired-processing)."""
        return self._queue_repo.pending_count(
            library_id=self._library_id,
            path_prefix=self._path_prefix,
            lease_minutes=self._lease_minutes,
        )

    def _emit_event(self, event: str, **fields: object) -> None:
        """Emit a structured event in jsonl mode; no-op for human mode. Uses shared contract."""
        emit_event(
            self._output_mode,
            {"event": event, "stage": "search_sync", **fields},
        )

    def process_one_batch(self) -> dict[str, int | bool]:
        """
        Claim and process exactly one batch from the queue.

        Returns {"processed": bool, "synced": int, "skipped": int}.
        processed=False means the queue was empty (no work claimed).

        Used by the API endpoint POST /v1/search-sync/process-batch so the server
        can drive sync without the CLI needing direct DB or Quickwit access.
        """
        if not self._quickwit.enabled:
            return {"processed": False, "synced": 0, "skipped": 0}

        self._quickwit.ensure_index_for_library(self._library_id)
        self._quickwit.ensure_scene_index_for_library(self._library_id)

        batch = self._queue_repo.claim_batch(
            self._batch_size,
            library_id=self._library_id,
            path_prefix=self._path_prefix,
            lease_minutes=self._lease_minutes,
        )
        if not batch:
            return {"processed": False, "synced": 0, "skipped": 0}

        batch_status, _ = self._ingest_one_batch(batch)
        synced = sum(1 for v in batch_status.values() if v == "synced")
        skipped = sum(1 for v in batch_status.values() if v == "skipped")
        return {"processed": True, "synced": synced, "skipped": skipped}

    def run_once(
        self,
        progress_callback: object | None = None,
    ) -> dict[str, int]:
        """
        Drain the queue until empty.

        If progress_callback is provided, it is called after each batch with
        (synced: int, skipped: int, batches: int).

        Returns {"synced": N, "skipped": M, "batches": B} where:
        - synced: number of unique assets successfully ingested to Quickwit
        - skipped: number of unique assets that were marked synced without ingesting
          (missing asset or no metadata)
        - batches: number of claim batches processed
        """
        # Global asset_status tracks deduplication across batches: "synced" wins over "skipped".
        global_asset_status: dict[str, str] = {}
        synced = 0
        skipped = 0
        batches = 0
        cb = progress_callback if callable(progress_callback) else None

        if not self._quickwit.enabled:
            logger.info("Quickwit disabled; skipping search sync run for library_id=%s", self._library_id)
            return {"synced": 0, "skipped": 0, "batches": 0}

        # Ensure index exists before ingesting
        self._quickwit.ensure_index_for_library(self._library_id)
        self._quickwit.ensure_scene_index_for_library(self._library_id)

        self._emit_event(
            "start",
            library_id=self._library_id,
            path_prefix=self._path_prefix or "",
        )

        while True:
            batch = self._queue_repo.claim_batch(
                self._batch_size,
                library_id=self._library_id,
                path_prefix=self._path_prefix,
                lease_minutes=self._lease_minutes,
            )
            if not batch:
                break
            batches += 1

            batch_status, _ = self._ingest_one_batch(batch)

            # Merge into global tracker: "synced" takes priority over "skipped".
            for asset_id, status in batch_status.items():
                if status == "synced" or global_asset_status.get(asset_id) != "synced":
                    global_asset_status[asset_id] = status

            synced = sum(1 for v in global_asset_status.values() if v == "synced")
            skipped = sum(1 for v in global_asset_status.values() if v == "skipped")
            if cb:
                cb(synced, skipped, batches)
            self._emit_event(
                "batch",
                library_id=self._library_id,
                path_prefix=self._path_prefix or "",
                synced=synced,
                skipped=skipped,
                batches=batches,
            )

        summary = {"synced": synced, "skipped": skipped, "batches": batches}
        self._emit_event("complete", library_id=self._library_id, **summary)
        return summary

    def _ingest_one_batch(
        self, batch: list
    ) -> tuple[dict[str, str], list[str]]:
        """
        Build docs for a claimed batch, ingest to Quickwit, and mark rows synced.

        Returns (asset_status, sync_ids) where asset_status maps asset_id →
        "synced"|"skipped" for the assets in this batch.
        """
        asset_status: dict[str, str] = {}
        docs: list[dict] = []
        scene_docs: list[dict] = []
        sync_ids: list[str] = []

        library = self._library_repo.get_by_id(self._library_id)
        scene_repo = VideoSceneRepository(self._session)
        for row in batch:
            if row.scene_id:
                # Scene document path
                scene = scene_repo.get_by_id(row.scene_id)
                asset = self._asset_repo.get_by_id(row.asset_id)
                if scene is None or asset is None:
                    sync_ids.append(row.sync_id)
                    if asset_status.get(row.asset_id) != "synced":
                        asset_status[row.asset_id] = "skipped"
                    continue
                assert asset.library_id == self._library_id, (
                    f"scene row {row.sync_id}: asset {asset.asset_id} belongs to library "
                    f"{asset.library_id}, not {self._library_id} — cross-library claim leak"
                )
                scene_doc = self._build_scene_document(scene, asset, library)
                scene_docs.append(scene_doc)
                sync_ids.append(row.sync_id)
                asset_status[row.asset_id] = "synced"
            else:
                # Asset document path — get_by_id returns None for trashed assets
                asset = self._asset_repo.get_by_id(row.asset_id)
                asset_id = row.asset_id
                if asset is None:
                    logger.warning(
                        "search_sync_queue row %s references missing asset_id=%s; marking synced",
                        row.sync_id,
                        row.asset_id,
                    )
                    sync_ids.append(row.sync_id)
                    if asset_status.get(asset_id) != "synced":
                        asset_status[asset_id] = "skipped"
                    continue

                assert asset.library_id == self._library_id, (
                    f"asset row {row.sync_id}: asset {asset.asset_id} belongs to library "
                    f"{asset.library_id}, not {self._library_id} — cross-library claim leak"
                )
                lib = self._library_repo.get_by_id(asset.library_id)
                vision_model_id = lib.vision_model_id if lib else ""
                model_version = "1"

                meta: AssetMetadata | None = self._meta_repo.get(
                    asset_id=asset.asset_id,
                    model_id=vision_model_id,
                    model_version=model_version,
                )
                if meta is None:
                    logger.debug(
                        "No AI metadata for asset_id=%s model=%s version=%s; skipping",
                        asset.asset_id,
                        vision_model_id,
                        model_version,
                    )
                    sync_ids.append(row.sync_id)
                    if asset_status.get(asset_id) != "synced":
                        asset_status[asset_id] = "skipped"
                    continue

                doc = self._build_document(asset, meta)
                docs.append(doc)
                sync_ids.append(row.sync_id)
                asset_status[asset_id] = "synced"

        if docs:
            self._quickwit.ingest_documents_for_library(self._library_id, docs)
        if scene_docs:
            self._quickwit.ingest_scene_documents_for_library(self._library_id, scene_docs)
        if sync_ids:
            self._queue_repo.mark_synced(sync_ids)

        return asset_status, sync_ids

    def _build_document(self, asset: Asset, meta: AssetMetadata) -> dict:
        """
        Build a Quickwit document for the given asset + metadata.
        """
        data = meta.data or {}
        description = data.get("description", "")
        tags = data.get("tags") or []

        # Quickwit expects timestamps as unix seconds when using input_formats ["unix_timestamp"].
        capture_ts = None
        if asset.taken_at:
            capture_ts = int(asset.taken_at.timestamp())

        indexed_at = int(utcnow().timestamp())

        return {
            "id": asset.asset_id,
            "asset_id": asset.asset_id,
            "library_id": asset.library_id,
            "rel_path": asset.rel_path,
            "path_tokens": _path_to_tokens(asset.rel_path),
            "media_type": asset.media_type,
            "description": description,
            "tags": tags,
            "capture_ts": capture_ts,
            "camera_make": asset.camera_make,
            "camera_model": asset.camera_model,
            "gps_lat": asset.gps_lat,
            "gps_lon": asset.gps_lon,
            "searchable": True,
            "model_id": meta.model_id,
            "model_version": meta.model_version,
            "indexed_at": indexed_at,
        }

    def _build_scene_document(
        self, scene: VideoScene, asset: Asset, library: Library | None
    ) -> dict:
        model_id = library.vision_model_id if library else ""
        model_version = "1"
        indexed_at = int(utcnow().timestamp())
        return {
            "id": scene.scene_id,
            "scene_id": scene.scene_id,
            "asset_id": asset.asset_id,
            "library_id": asset.library_id,
            "rel_path": asset.rel_path,
            "start_ms": scene.start_ms,
            "end_ms": scene.end_ms,
            "rep_frame_ms": scene.rep_frame_ms,
            "thumbnail_key": scene.thumbnail_key,
            "duration_sec": asset.duration_sec
            or (asset.duration_ms / 1000.0 if asset.duration_ms else None),
            "description": scene.description or "",
            "tags": scene.tags or [],
            "sharpness_score": scene.sharpness_score,
            "keep_reason": scene.keep_reason,
            "model_id": model_id,
            "model_version": model_version,
            "indexed_at": indexed_at,
        }

