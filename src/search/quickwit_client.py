from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

import requests

from src.core.config import get_settings

logger = logging.getLogger(__name__)

INGEST_BATCH_SIZE = 500


class QuickwitClient:
    """
    Minimal Quickwit HTTP client for index management and ingest.

    Uses the quickwit_url from application settings. This client is
    intentionally small and focused on the needs of the search sync worker.
    """

    def __init__(self, schema_dir: Path | None = None) -> None:
        settings = get_settings()
        self._base_url = settings.quickwit_url.rstrip("/")
        self._enabled = settings.quickwit_enabled
        self._schema_dir = schema_dir or Path("quickwit")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def index_id_for_library(self, library_id: str) -> str:
        """Return the Quickwit index id for a given library."""
        return f"lumiverb_{library_id}"

    def _schema_path(self) -> Path:
        return self._schema_dir / "asset_index_schema.json"

    def ensure_index_for_library(self, library_id: str) -> None:
        """
        Ensure the Quickwit index for this library exists, creating it if needed.

        Index id is derived from the library_id and substituted into the schema
        template before sending to Quickwit.
        """
        if not self._enabled:
            logger.debug("Quickwit disabled; skipping ensure_index_for_library(%s)", library_id)
            return

        index_id = self.index_id_for_library(library_id)

        # Cheap existence check
        exists_resp = requests.get(f"{self._base_url}/api/v1/indexes/{index_id}", timeout=5)
        if exists_resp.status_code == 200:
            return
        if exists_resp.status_code not in (404, 400):
            logger.warning(
                "Quickwit index existence check failed for %s: %s %s",
                index_id,
                exists_resp.status_code,
                exists_resp.text,
            )

        schema_path = self._schema_path()
        if not schema_path.exists():
            raise FileNotFoundError(f"Quickwit schema not found: {schema_path}")

        raw = schema_path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid Quickwit schema JSON: {schema_path}") from e

        data["index_id"] = index_id

        resp = requests.post(
            f"{self._base_url}/api/v1/indexes",
            headers={"Content-Type": "application/json"},
            data=json.dumps(data),
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Quickwit index create failed for {index_id}: {resp.status_code} {resp.text}"
            )

    def ingest_documents_for_library(
        self,
        library_id: str,
        docs: Iterable[dict],
    ) -> None:
        """
        Ingest documents into the library's index in batches.

        Documents are sent as NDJSON, batched to avoid HTTP body size limits.
        """
        if not self._enabled:
            logger.debug("Quickwit disabled; skipping ingest for library_id=%s", library_id)
            return

        docs_list = list(docs)
        if not docs_list:
            return

        index_id = self.index_id_for_library(library_id)
        for i in range(0, len(docs_list), INGEST_BATCH_SIZE):
            batch = docs_list[i : i + INGEST_BATCH_SIZE]
            ndjson = "\n".join(json.dumps(d) for d in batch) + "\n"

            resp = requests.post(
                f"{self._base_url}/api/v1/{index_id}/ingest?commit=force",
                headers={"Content-Type": "application/json"},
                data=ndjson,
                timeout=30,
            )
            if resp.status_code not in (200, 202):
                raise RuntimeError(
                    f"Quickwit ingest failed for index {index_id}: {resp.status_code} {resp.text}"
                )

    def search(
        self,
        library_id: str,
        query: str,
        max_hits: int = 20,
        start_offset: int = 0,
    ) -> list[dict]:
        """
        BM25 full-text search against the library's Quickwit index.

        Returns list of hit dicts. Raises on HTTP error.
        If Quickwit is disabled, returns empty list.
        """
        if not self._enabled:
            return []

        index_id = self.index_id_for_library(library_id)
        resp = requests.post(
            f"{self._base_url}/api/v1/{index_id}/search",
            json={
                "query": query,
                "max_hits": max_hits,
                "start_offset": start_offset,
                "sort_by": "_score",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_hits = data.get("hits", [])
        results: list[dict] = []
        for hit in raw_hits:
            # Quickwit may return doc in _source or flat
            source_doc = hit.get("_source", hit)
            # Quickwit does not expose BM25 scores in the response body; results are sorted by relevance but score value is unavailable.
            score = 0.0
            results.append(
                {
                    "asset_id": source_doc.get("asset_id", ""),
                    "rel_path": source_doc.get("rel_path", ""),
                    "thumbnail_key": source_doc.get("thumbnail_key"),
                    "proxy_key": source_doc.get("proxy_key"),
                    "camera_make": source_doc.get("camera_make"),
                    "camera_model": source_doc.get("camera_model"),
                    "description": source_doc.get("description", ""),
                    "tags": source_doc.get("tags", []),
                    "score": float(score),
                    "source": "quickwit",
                }
            )
        return results

