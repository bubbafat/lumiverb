from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlmodel import Session

from src.models.tenant import Asset, AssetMetadata
from src.repository.tenant import (
    AssetMetadataRepository,
    AssetRepository,
    SearchSyncQueueRepository,
)
from src.search.quickwit_client import QuickwitClient
from src.workers.vision import VISION_MODEL_ID, VISION_MODEL_VERSION

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    ) -> None:
        self._session = session
        self._library_id = library_id
        self._batch_size = batch_size
        self._asset_repo = AssetRepository(session)
        self._meta_repo = AssetMetadataRepository(session)
        self._queue_repo = SearchSyncQueueRepository(session)
        self._quickwit = quickwit or QuickwitClient()

    def run_once(self) -> int:
        """
        Drain the queue until empty.

        Returns the total number of rows processed.
        """
        total = 0
        if not self._quickwit.enabled:
            logger.info("Quickwit disabled; skipping search sync run for library_id=%s", self._library_id)
            return 0

        # Ensure index exists before ingesting
        self._quickwit.ensure_index_for_library(self._library_id)

        while True:
            batch = self._queue_repo.claim_batch(self._batch_size)
            if not batch:
                break

            docs: list[dict] = []
            sync_ids: list[str] = []

            for row in batch:
                asset = self._asset_repo.get_by_id(row.asset_id)
                if asset is None:
                    logger.warning(
                        "search_sync_queue row %s references missing asset_id=%s; marking synced",
                        row.sync_id,
                        row.asset_id,
                    )
                    sync_ids.append(row.sync_id)
                    continue

                # For now we target the Moondream vision metadata only.
                meta: AssetMetadata | None = self._meta_repo.get(
                    asset_id=asset.asset_id,
                    model_id=VISION_MODEL_ID,
                    model_version=VISION_MODEL_VERSION,
                )
                if meta is None:
                    logger.debug(
                        "No AI metadata for asset_id=%s model=%s version=%s; skipping",
                        asset.asset_id,
                        VISION_MODEL_ID,
                        VISION_MODEL_VERSION,
                    )
                    sync_ids.append(row.sync_id)
                    continue

                doc = self._build_document(asset, meta)
                docs.append(doc)
                sync_ids.append(row.sync_id)

            if docs:
                self._quickwit.ingest_documents_for_library(self._library_id, docs)

            if sync_ids:
                self._queue_repo.mark_synced(sync_ids)

            total += len(sync_ids)

        return total

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

        indexed_at = int(_utcnow().timestamp())

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

