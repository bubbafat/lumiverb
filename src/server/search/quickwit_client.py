from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Iterable

import requests

from src.server.config import get_settings
from src.server.search import indexes as search_indexes

logger = logging.getLogger(__name__)

INGEST_BATCH_SIZE = 500

# Quickwit can answer an ingest into an index it has just made with 404 for a
# moment, and the sync callers swallow that, so the first sync into a new
# index would be lost until the sweep. After making an index the client asks
# it to take an empty ingest, backing off from _INDEX_READY_FIRST_DELAY_S up to
# _INDEX_READY_MAX_DELAY_S between tries, for at most INDEX_READY_TIMEOUT_S in
# all; then it logs a warning and goes on (the sweep catches up).
INDEX_READY_TIMEOUT_S = 5.0
_INDEX_READY_FIRST_DELAY_S = 0.1
_INDEX_READY_MAX_DELAY_S = 1.0
_INDEX_READY_PROBE_TIMEOUT_S = 2.0


class QuickwitClient:
    """
    Minimal Quickwit HTTP client for index management and ingest.

    Uses per-tenant indexes: one asset index, one scene index and one
    transcript index per tenant. The library_id field in each document
    enables per-library filtering. Searches read each kind's live index;
    writes and deletes go to it and to one built or retired beside it
    (src/server/search/indexes.py).
    """

    def __init__(self, schema_dir: Path | None = None, where=None) -> None:
        settings = get_settings()
        self._base_url = settings.quickwit_url.rstrip("/")
        self._enabled = settings.quickwit_enabled
        self._schema_dir = schema_dir or Path("quickwit")
        # tenant_id → each kind's indexes (indexes.Where); the account's database's by default.
        self._where = where or search_indexes.cached

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _schema_path(self) -> Path:
        return self._schema_dir / "asset_index_schema.json"

    def _scene_schema_path(self) -> Path:
        return self._schema_dir / "scene_index_schema.json"

    def _transcript_schema_path(self) -> Path:
        return self._schema_dir / "transcript_index_schema.json"

    # ------------------------------------------------------------------
    # Index naming
    # ------------------------------------------------------------------

    # The original names (an account that never rebuilt an index still has them).
    def tenant_index_id(self, tenant_id: str) -> str:
        return search_indexes.base_id(tenant_id, "assets")

    def tenant_scene_index_id(self, tenant_id: str) -> str:
        return search_indexes.base_id(tenant_id, "scenes")

    def tenant_transcript_index_id(self, tenant_id: str) -> str:
        return search_indexes.base_id(tenant_id, "transcripts")

    def schema_path(self, kind: str) -> Path:
        return {"assets": self._schema_path, "scenes": self._scene_schema_path,
                "transcripts": self._transcript_schema_path}[kind]()

    def _live(self, tenant_id: str, kind: str) -> str:
        """The index a kind's searches read."""
        return self._where(tenant_id)[kind].live

    def _targets(self, tenant_id: str, kind: str) -> list[str]:
        """The indexes a kind's writes and deletes go to: its live one, and one built or retired beside it."""
        return self._where(tenant_id)[kind].targets()

    def tenant_index_state(self, tenant_id: str, timeout: float = 2.0) -> tuple[str, str]:
        """Whether the account's asset index answers: ("ok", ""), ("missing", "")
        or ("down", why in a few words). For the Admin page's Search row; nothing is changed."""
        try:
            resp = requests.get(f"{self._base_url}/api/v1/indexes/{self._live(tenant_id, 'assets')}", timeout=timeout)
        except requests.RequestException as exc:
            return "down", type(exc).__name__
        if resp.status_code == 200:
            return "ok", ""
        if resp.status_code in (400, 404):
            return "missing", ""
        return "down", f"it answered {resp.status_code}"

    # ------------------------------------------------------------------
    # Index lifecycle
    # ------------------------------------------------------------------

    def ensure_tenant_index(self, tenant_id: str) -> bool:
        """Ensure the account's live asset index exists. Returns True if it was
        made (it was missing): everything goes in again. One whose schema
        changed is rebuilt beside it by the sweep (search/sync.py), not here."""
        return self._ensure_index(self._live(tenant_id, "assets"), self._schema_path())

    def ensure_tenant_scene_index(self, tenant_id: str) -> bool:
        """Ensure the account's live scene index exists. Returns True if it was made."""
        return self._ensure_index(self._live(tenant_id, "scenes"), self._scene_schema_path())

    def ensure_tenant_transcript_index(self, tenant_id: str) -> bool:
        """Ensure the account's live transcript index exists. Returns True if it was made."""
        return self._ensure_index(self._live(tenant_id, "transcripts"), self._transcript_schema_path())

    def outdated(self, index_id: str, kind: str) -> bool:
        """Whether an index lacks fields the kind's schema has now (so it's
        rebuilt beside). False when it's missing or can't be read."""
        if not self._enabled:
            return False
        resp = requests.get(f"{self._base_url}/api/v1/indexes/{index_id}", timeout=5)
        return resp.status_code == 200 and self._schema_fields_changed(resp.json(), self.schema_path(kind))

    def make_index(self, index_id: str, kind: str) -> bool:
        """Make an index of the kind with its schema now, unless it's there. True if made."""
        return self._ensure_index(index_id, self.schema_path(kind))

    def drop_index(self, index_id: str) -> None:
        """Delete an index (one retired after a rebuild)."""
        self._delete_index(index_id)

    def _delete_index(self, index_id: str) -> None:
        if not self._enabled:
            return
        resp = requests.delete(f"{self._base_url}/api/v1/indexes/{index_id}", timeout=10)
        if resp.status_code in (200, 404):
            return
        logger.warning("Quickwit index delete failed for %s: %s %s", index_id, resp.status_code, resp.text)

    def _ensure_index(self, index_id: str, schema_path: Path) -> bool:
        """Ensure the index exists. Returns True if it was made. One that's
        there stays as it is, whatever its schema: never deleted here."""
        if not self._enabled:
            return False
        recreated = False
        exists_resp = requests.get(f"{self._base_url}/api/v1/indexes/{index_id}", timeout=5)
        if exists_resp.status_code == 200:
            return False
        elif exists_resp.status_code in (404, 400):
            # Missing (deleted, or Quickwit's data lost): what it held is
            # gone, so the caller indexes everything again.
            recreated = True
        else:
            # Can't tell: not taken for missing, so a flaky check never sets off a full reindex.
            raise RuntimeError(f"Quickwit index check failed for {index_id}: {exists_resp.status_code}")
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
        self._wait_until_ready(index_id)
        return recreated

    def _wait_until_ready(self, index_id: str) -> bool:
        """Wait, at most INDEX_READY_TIMEOUT_S, until a new index takes an ingest.

        The probe is an empty ingest without a commit: it adds nothing. 404 or
        no connection means not ready yet; any other answer means the index is
        there (a real ingest reports its own errors). Returns False, after a
        warning, if the bound runs out.
        """
        deadline = time.monotonic() + INDEX_READY_TIMEOUT_S
        delay = _INDEX_READY_FIRST_DELAY_S
        while True:
            remaining = deadline - time.monotonic()
            try:
                resp = requests.post(
                    f"{self._base_url}/api/v1/{index_id}/ingest",
                    headers={"Content-Type": "application/json"},
                    data="",
                    timeout=max(0.1, min(_INDEX_READY_PROBE_TIMEOUT_S, remaining)),
                )
                if resp.status_code != 404:
                    return True
                last = f"ingest answered {resp.status_code}"
            except requests.RequestException as exc:
                last = f"ingest failed: {exc}"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "Quickwit index %s not ready %.1fs after it was made (%s); "
                    "going on, the search sweep will index what this sync misses",
                    index_id, INDEX_READY_TIMEOUT_S, last,
                )
                return False
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _INDEX_READY_MAX_DELAY_S)

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
        """Ingest asset documents into the account's asset indexes (live, and one beside it)."""
        self._ingest_to_kind(tenant_id, "assets", docs)

    def ingest_tenant_scene_documents(self, tenant_id: str, docs: Iterable[dict]) -> None:
        """Ingest scene documents into the account's scene indexes."""
        self._ingest_to_kind(tenant_id, "scenes", docs)

    def ingest_tenant_transcript_documents(self, tenant_id: str, docs: Iterable[dict]) -> None:
        """Ingest transcript segment documents into the account's transcript indexes."""
        self._ingest_to_kind(tenant_id, "transcripts", docs)

    def _ingest_to_kind(self, tenant_id: str, kind: str, docs: Iterable[dict]) -> None:
        if not self._enabled:
            return
        docs = list(docs)
        for index_id in self._targets(tenant_id, kind):
            self._ingest_to_index(index_id, docs)

    def ingest_into(self, index_id: str, docs: Iterable[dict]) -> None:
        """Ingest documents into one index (a rebuild's, as the sweep fills it)."""
        self._ingest_to_index(index_id, docs)

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

    # Down-weight applied to scene and transcript scores so an asset
    # match always outranks an equivalent-rank scene/transcript match.
    # First pass at 0.5 wasn't aggressive enough — a scene at rank 0
    # (score 0.5 after penalty) tied with asset rank 1 (1/(1+1) = 0.5)
    # and landed at #2. With 0.1 the scene rank-0 score becomes 0.1
    # which ties with asset rank 9 (1/(1+9) = 0.1), so a single scene
    # hit lands around position 10 — present, but firmly subordinate
    # to the photo matches at the top. A genuinely strong scene match
    # (rank 0 with no competing asset hits) still surfaces because
    # the asset score floor at deeper ranks decays naturally.
    SCENE_RANK_PENALTY = 0.1
    TRANSCRIPT_RANK_PENALTY = 0.1

    def search_tenant(
        self,
        tenant_id: str,
        query: str,
        library_ids: list[str] | None = None,
        max_hits: int = 20,
        start_offset: int = 0,
    ) -> list[dict]:
        """BM25 search on the per-tenant asset index. Optionally filter by library_id(s).

        Quickwit's search response returns hits in BM25 relevance order
        but does NOT include a `_score` field per hit (that's an
        Elasticsearch convention, not Quickwit's). Without an explicit
        score the existing merge logic in `_run_quickwit_search` (which
        does `if score > scores[aid]`) collapses to comparing zeros and
        loses the relevance signal. We synthesize a position-based
        score: `1.0 / (1 + rank)` so rank 0 = 1.0, rank 1 = 0.5, rank 9
        = 0.1, etc. Strictly decreasing, all positive, comparable across
        per-index calls.
        """
        effective_query = self._apply_library_filter(query, library_ids)
        index_id = self._live(tenant_id, "assets") if self._enabled else self.tenant_index_id(tenant_id)
        raw_hits = self._search_index(index_id, effective_query, max_hits, start_offset)
        results: list[dict] = []
        for rank, hit in enumerate(raw_hits):
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
                "score": 1.0 / (1 + rank),
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
        """BM25 search on the per-tenant scene index. Optionally
        filter by library_id(s). Position-based scoring — see
        ``search_tenant`` for the rationale."""
        effective_query = self._apply_library_filter(query, library_ids)
        index_id = self._live(tenant_id, "scenes") if self._enabled else self.tenant_scene_index_id(tenant_id)
        raw_hits = self._search_index(index_id, effective_query, max_hits, start_offset)
        results: list[dict] = []
        for rank, hit in enumerate(raw_hits):
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
                "score": (1.0 / (1 + rank)) * self.SCENE_RANK_PENALTY,
                "source": "quickwit_scenes",
            })
        return results

    def search_tenant_transcripts(
        self,
        tenant_id: str,
        query: str,
        library_ids: list[str] | None = None,
        max_hits: int = 60,
        start_offset: int = 0,
    ) -> list[dict]:
        """BM25 search on the per-tenant transcript index. Returns
        per-segment hits with position-based scoring — see
        ``search_tenant`` for the rationale."""
        effective_query = self._apply_library_filter(query, library_ids)
        index_id = self._live(tenant_id, "transcripts") if self._enabled else self.tenant_transcript_index_id(tenant_id)
        raw_hits = self._search_index(index_id, effective_query, max_hits, start_offset)
        results: list[dict] = []
        for rank, hit in enumerate(raw_hits):
            doc = hit.get("_source", hit)
            results.append({
                "asset_id": doc.get("asset_id", ""),
                "library_id": doc.get("library_id", ""),
                "rel_path": doc.get("rel_path", ""),
                "media_type": doc.get("media_type", "video"),
                "start_ms": doc.get("start_ms"),
                "end_ms": doc.get("end_ms"),
                "text": doc.get("text", ""),
                "language": doc.get("language", ""),
                "score": (1.0 / (1 + rank)) * self.TRANSCRIPT_RANK_PENALTY,
                "source": "quickwit_transcripts",
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
    # Every delete goes to each index of its kind a write would go to: the
    # live one, and one built or retired beside it (indexes.Where.targets).

    def _delete_where(self, index_ids: list[str], field: str, values: list[str], *, what: str,
                      batch: int = 500, timeout: float = 30) -> None:
        """Delete the documents whose field is one of values from each index,
        `batch` values a request (an OR'd delete-by-query). Best effort: a
        failure is logged. Async at Quickwit's side (delete-tasks): documents
        ingested just after may briefly coexist with the old ones, and the
        per-asset_id merge in `_run_quickwit_search` collapses any duplicates."""
        if not self._enabled or not values:
            return
        for i in range(0, len(values), batch):
            chunk = values[i : i + batch]
            query = " OR ".join(f'{field}:"{v}"' for v in chunk)
            for index_id in index_ids:
                try:
                    resp = requests.post(f"{self._base_url}/api/v1/{index_id}/delete-tasks",
                                         json={"query": query}, timeout=timeout)
                    if resp.status_code not in (200, 201, 202):
                        logger.warning("Quickwit %s delete failed for %s (%d ids): %s %s",
                                       what, index_id, len(chunk), resp.status_code, resp.text)
                except requests.RequestException as exc:
                    logger.warning("Quickwit %s delete request failed for %s (%d ids): %s",
                                   what, index_id, len(chunk), exc)

    def _every_index(self, tenant_id: str) -> list[str]:
        """Each index of each kind a write would go to."""
        return [i for kind in search_indexes.KINDS for i in self._targets(tenant_id, kind)]

    def delete_from(self, index_id: str, field: str, values: list[str]) -> None:
        """Delete documents whose field is one of values from one index (a
        rebuild's, before the sweep puts them in again)."""
        self._delete_where([index_id], field, values, what="rebuild")

    def delete_tenant_transcript_documents(self, tenant_id: str, asset_id: str) -> None:
        """Delete all transcript segment documents for an asset from the transcript indexes."""
        if not self._enabled:
            return
        self._delete_where(self._targets(tenant_id, "transcripts"), "asset_id", [asset_id], what="transcript",
                           timeout=10)

    def delete_tenant_documents_by_asset_id(self, tenant_id: str, asset_id: str) -> None:
        """Best-effort delete documents for an asset from every tenant index."""
        if not self._enabled:
            return
        self._delete_where(self._every_index(tenant_id), "asset_id", [asset_id], what="asset", timeout=10)

    def delete_asset_index_documents_by_asset_ids(self, tenant_id: str, asset_ids: list[str]) -> None:
        """Bulk-delete asset-index docs for a list of asset_ids, 500 a
        request. Used by the search-sync sweep to wipe stale duplicates
        before re-ingesting: Quickwit's HTTP ingest does NOT upsert (you
        can't update a doc by primary key), so without an explicit delete
        each re-sync pass would accumulate another copy of every doc. This
        is the dedupe gate."""
        if not self._enabled or not asset_ids:
            return
        self._delete_where(self._targets(tenant_id, "assets"), "asset_id", asset_ids, what="bulk")

    def delete_scene_index_documents_by_asset_ids(self, tenant_id: str, asset_ids: list[str]) -> None:
        """Bulk-delete scene-index docs for a list of asset_ids. Mirror
        of `delete_asset_index_documents_by_asset_ids` for the scene
        index — used by the sync sweep before scene ingest."""
        if not self._enabled or not asset_ids:
            return
        self._delete_where(self._targets(tenant_id, "scenes"), "asset_id", asset_ids, what="bulk scene")

    def delete_scene_index_documents_by_scene_ids(self, tenant_id: str, scene_ids: list[str]) -> None:
        """Bulk-delete these scenes' docs from the scene index (a clip's
        other scenes stay), 500 a request: inline scene sync's dedupe gate."""
        if not self._enabled or not scene_ids:
            return
        self._delete_where(self._targets(tenant_id, "scenes"), "scene_id", scene_ids, what="bulk scene")

    def delete_transcript_index_documents_by_asset_ids(self, tenant_id: str, asset_ids: list[str]) -> None:
        """Bulk-delete transcript-index docs for a list of asset_ids, 500 a
        request: the sync sweep's dedupe gate before transcript ingest."""
        if not self._enabled or not asset_ids:
            return
        self._delete_where(self._targets(tenant_id, "transcripts"), "asset_id", asset_ids, what="bulk transcript")

    def delete_tenant_documents_by_asset_ids(self, tenant_id: str, asset_ids: list[str]) -> None:
        """Best-effort: delete these assets' documents from all three kinds'
        indexes (assets, scenes, transcript segments), 500 ids a request.
        For many clips at once (archiving a folder, trashing a selection)."""
        if not self._enabled or not asset_ids:
            return
        self._delete_where(self._every_index(tenant_id), "asset_id", asset_ids, what="bulk")

    def delete_tenant_documents_by_library_id(self, tenant_id: str, library_id: str) -> None:
        """Best-effort delete all documents for a library from every tenant index."""
        if not self._enabled:
            return
        self._delete_where(self._every_index(tenant_id), "library_id", [library_id], what="library", timeout=10)
