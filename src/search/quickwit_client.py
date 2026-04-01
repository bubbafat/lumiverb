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

    Uses per-tenant indexes: one asset index and one scene index per tenant.
    The library_id field in each document enables per-library filtering.
    """

    def __init__(self, schema_dir: Path | None = None) -> None:
        settings = get_settings()
        self._base_url = settings.quickwit_url.rstrip("/")
        self._enabled = settings.quickwit_enabled
        self._schema_dir = schema_dir or Path("quickwit")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _schema_path(self) -> Path:
        return self._schema_dir / "asset_index_schema.json"

    def _scene_schema_path(self) -> Path:
        return self._schema_dir / "scene_index_schema.json"

    # ------------------------------------------------------------------
    # Index naming
    # ------------------------------------------------------------------

    def tenant_index_id(self, tenant_id: str) -> str:
        return f"lumiverb_tenant_{tenant_id}"

    def tenant_scene_index_id(self, tenant_id: str) -> str:
        return f"lumiverb_tenant_{tenant_id}_scenes"

    # ------------------------------------------------------------------
    # Index lifecycle
    # ------------------------------------------------------------------

    def ensure_tenant_index(self, tenant_id: str) -> bool:
        """Ensure the per-tenant asset index exists. Returns True if recreated."""
        return self._ensure_index(self.tenant_index_id(tenant_id), self._schema_path())

    def ensure_tenant_scene_index(self, tenant_id: str) -> bool:
        """Ensure the per-tenant scene index exists. Returns True if recreated."""
        return self._ensure_index(self.tenant_scene_index_id(tenant_id), self._scene_schema_path())

    def recreate_tenant_indexes(self, tenant_id: str) -> None:
        """Delete and recreate both tenant indexes (asset + scene) with current schema.

        Use when the schema has changed (new fields added). All documents
        must be re-indexed after this call.
        """
        for index_id, schema_path in [
            (self.tenant_index_id(tenant_id), self._schema_path()),
            (self.tenant_scene_index_id(tenant_id), self._scene_schema_path()),
        ]:
            self._delete_index(index_id)
            self._ensure_index(index_id, schema_path)

    def _delete_index(self, index_id: str) -> None:
        if not self._enabled:
            return
        resp = requests.delete(f"{self._base_url}/api/v1/indexes/{index_id}", timeout=10)
        if resp.status_code in (200, 404):
            return
        logger.warning("Quickwit index delete failed for %s: %s %s", index_id, resp.status_code, resp.text)

    def _ensure_index(self, index_id: str, schema_path: Path, *, auto_recreate: bool = True) -> bool:
        """Ensure index exists with current schema. Returns True if (re)created."""
        if not self._enabled:
            return False
        recreated = False
        exists_resp = requests.get(f"{self._base_url}/api/v1/indexes/{index_id}", timeout=5)
        if exists_resp.status_code == 200:
            if auto_recreate and self._schema_fields_changed(exists_resp.json(), schema_path):
                logger.info("Quickwit index %s has outdated schema — recreating", index_id)
                self._delete_index(index_id)
                recreated = True
                # Fall through to create below
            else:
                return False
        if exists_resp.status_code not in (200, 404, 400):
            logger.warning("Quickwit index check failed for %s: %s %s", index_id, exists_resp.status_code, exists_resp.text)
        if not schema_path.exists():
            raise FileNotFoundError(f"Quickwit schema not found: {schema_path}")
        data = json.loads(schema_path.read_text(encoding="utf-8"))
        data["index_id"] = index_id
        resp = requests.post(
            f"{self._base_url}/api/v1/indexes",
            headers={"Content-Type": "application/json"},
            data=json.dumps(data),
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Quickwit index create failed for {index_id}: {resp.status_code} {resp.text}")
        return recreated

    @staticmethod
    def _schema_fields_changed(index_metadata: dict, schema_path: Path) -> bool:
        """Compare expected schema fields to the live index. Returns True if a recreate is needed."""
        try:
            expected = json.loads(schema_path.read_text(encoding="utf-8"))
            expected_fields = {f["name"] for f in expected.get("doc_mapping", {}).get("field_mappings", [])}
            # Quickwit returns index metadata with doc_mapping.field_mappings
            live_mappings = index_metadata.get("index_config", {}).get("doc_mapping", {}).get("field_mappings", [])
            live_fields = {f["name"] for f in live_mappings}
            missing = expected_fields - live_fields
            if missing:
                logger.info("Quickwit index missing fields: %s", missing)
                return True
            return False
        except Exception as exc:
            logger.warning("Could not compare Quickwit schema: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def ingest_tenant_documents(self, tenant_id: str, docs: Iterable[dict]) -> None:
        """Ingest asset documents into the per-tenant index."""
        self._ingest_to_index(self.tenant_index_id(tenant_id), docs)

    def ingest_tenant_scene_documents(self, tenant_id: str, docs: Iterable[dict]) -> None:
        """Ingest scene documents into the per-tenant scene index."""
        self._ingest_to_index(self.tenant_scene_index_id(tenant_id), docs)

    def _ingest_to_index(self, index_id: str, docs: Iterable[dict]) -> None:
        if not self._enabled:
            return
        docs_list = list(docs)
        if not docs_list:
            return
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
                raise RuntimeError(f"Quickwit ingest failed for {index_id}: {resp.status_code} {resp.text}")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_library_filter(query: str, library_ids: list[str] | None) -> str:
        if not library_ids:
            return query
        if len(library_ids) == 1:
            return f'library_id:"{library_ids[0]}" AND ({query})'
        lib_clause = " OR ".join(f'library_id:"{lid}"' for lid in library_ids)
        return f"({lib_clause}) AND ({query})"

    def search_tenant(
        self,
        tenant_id: str,
        query: str,
        library_ids: list[str] | None = None,
        max_hits: int = 20,
        start_offset: int = 0,
    ) -> list[dict]:
        """BM25 search on the per-tenant asset index. Optionally filter by library_id(s)."""
        effective_query = self._apply_library_filter(query, library_ids)
        index_id = self.tenant_index_id(tenant_id)
        raw_hits = self._search_index(index_id, effective_query, max_hits, start_offset)
        results: list[dict] = []
        for hit in raw_hits:
            doc = hit.get("_source", hit)
            results.append({
                "asset_id": doc.get("asset_id", ""),
                "library_id": doc.get("library_id", ""),
                "rel_path": doc.get("rel_path", ""),
                "thumbnail_key": doc.get("thumbnail_key"),
                "proxy_key": doc.get("proxy_key"),
                "camera_make": doc.get("camera_make"),
                "camera_model": doc.get("camera_model"),
                "description": doc.get("description", ""),
                "tags": doc.get("tags", []),
                "score": 0.0,
                "source": "quickwit",
            })
        return results

    def search_tenant_scenes(
        self,
        tenant_id: str,
        query: str,
        library_ids: list[str] | None = None,
        max_hits: int = 20,
        start_offset: int = 0,
    ) -> list[dict]:
        """BM25 search on the per-tenant scene index. Optionally filter by library_id(s)."""
        effective_query = self._apply_library_filter(query, library_ids)
        index_id = self.tenant_scene_index_id(tenant_id)
        raw_hits = self._search_index(index_id, effective_query, max_hits, start_offset)
        results: list[dict] = []
        for hit in raw_hits:
            doc = hit.get("_source", hit)
            results.append({
                "scene_id": doc.get("scene_id", ""),
                "asset_id": doc.get("asset_id", ""),
                "library_id": doc.get("library_id", ""),
                "rel_path": doc.get("rel_path", ""),
                "start_ms": doc.get("start_ms"),
                "end_ms": doc.get("end_ms"),
                "rep_frame_ms": doc.get("rep_frame_ms"),
                "thumbnail_key": doc.get("thumbnail_key"),
                "duration_sec": doc.get("duration_sec"),
                "description": doc.get("description", ""),
                "tags": doc.get("tags", []),
                "score": 0.0,
                "source": "quickwit_scenes",
            })
        return results

    def _search_index(self, index_id: str, query: str, max_hits: int, start_offset: int) -> list[dict]:
        if not self._enabled:
            return []
        resp = requests.post(
            f"{self._base_url}/api/v1/{index_id}/search",
            json={"query": query, "max_hits": max_hits, "start_offset": start_offset, "sort_by": "_score"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("hits", [])

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_tenant_documents_by_asset_id(self, tenant_id: str, asset_id: str) -> None:
        """Best-effort delete documents for an asset from both tenant indexes."""
        if not self._enabled:
            return
        query = f'asset_id:"{asset_id}"'
        for index_id in (self.tenant_index_id(tenant_id), self.tenant_scene_index_id(tenant_id)):
            try:
                resp = requests.post(f"{self._base_url}/api/v1/{index_id}/delete-tasks", json={"query": query}, timeout=10)
                if resp.status_code not in (200, 201, 202):
                    logger.warning("Quickwit delete-tasks failed for %s asset_id=%s: %s %s", index_id, asset_id, resp.status_code, resp.text)
            except requests.RequestException as exc:
                logger.warning("Quickwit delete-tasks request failed for %s asset_id=%s: %s", index_id, asset_id, exc)

    def delete_tenant_documents_by_library_id(self, tenant_id: str, library_id: str) -> None:
        """Best-effort delete all documents for a library from both tenant indexes."""
        if not self._enabled:
            return
        query = f'library_id:"{library_id}"'
        for index_id in (self.tenant_index_id(tenant_id), self.tenant_scene_index_id(tenant_id)):
            try:
                resp = requests.post(f"{self._base_url}/api/v1/{index_id}/delete-tasks", json={"query": query}, timeout=10)
                if resp.status_code not in (200, 201, 202):
                    logger.warning("Quickwit delete-tasks failed for %s library_id=%s: %s %s", index_id, library_id, resp.status_code, resp.text)
            except requests.RequestException as exc:
                logger.warning("Quickwit delete-tasks request failed for %s library_id=%s: %s", index_id, library_id, exc)
