from __future__ import annotations

import logging
from typing import Iterable

from sqlmodel import Session

from src.core.io_utils import normalize_path_prefix
from src.core.utils import utcnow
from src.models.registry import model_version_for_provenance
from src.models.tenant import Asset, AssetMetadata
from src.repository.tenant import (
    AssetMetadataRepository,
    AssetRepository,
    LibraryRepository,
    SearchSyncQueueRepository,
)
from src.search.quickwit_client import QuickwitClient

logger = logging.getLogger(__name__)


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

    def pending_count(self) -> int:
        """Return number of unsynced rows in search_sync_queue for this library/path."""
        return self._queue_repo.pending_count(
            library_id=self._library_id,
            path_prefix=self._path_prefix,
        )

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
        asset_status: dict[str, str] = {}
        synced = 0
        skipped = 0
        batches = 0
        cb = progress_callback if callable(progress_callback) else None

        if not self._quickwit.enabled:
            logger.info("Quickwit disabled; skipping search sync run for library_id=%s", self._library_id)
            return {"synced": 0, "skipped": 0, "batches": 0}

        # Ensure index exists before ingesting
        self._quickwit.ensure_index_for_library(self._library_id)

        while True:
            batch = self._queue_repo.claim_batch(
                self._batch_size,
                library_id=self._library_id,
                path_prefix=self._path_prefix,
            )
            if not batch:
                break
            batches += 1

            docs: list[dict] = []
            sync_ids: list[str] = []

            for row in batch:
                asset = self._asset_repo.get_by_id(row.asset_id)
                asset_id = row.asset_id
                if asset is None:
                    logger.warning(
                        "search_sync_queue row %s references missing asset_id=%s; marking synced",
                        row.sync_id,
                        row.asset_id,
                    )
                    sync_ids.append(row.sync_id)
                    # Only mark skipped if we haven't already marked this asset as synced.
                    if asset_status.get(asset_id) != "synced":
                        asset_status[asset_id] = "skipped"
                    continue

                library = self._library_repo.get_by_id(asset.library_id)
                vision_model_id = library.vision_model_id if library else "moondream"
                model_version = model_version_for_provenance(vision_model_id)

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

            if sync_ids:
                self._queue_repo.mark_synced(sync_ids)

            synced = sum(1 for v in asset_status.values() if v == "synced")
            skipped = sum(1 for v in asset_status.values() if v == "skipped")
            if cb:
                cb(synced, skipped, batches)

        return {"synced": synced, "skipped": skipped, "batches": batches}

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

