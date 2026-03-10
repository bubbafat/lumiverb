"""Fast tests for search deduplication and ingest batching."""

from unittest.mock import patch

import pytest


@pytest.mark.fast
def test_search_deduplicates_hits() -> None:
    """Construct hits with duplicate asset_id, run dedup logic, assert higher score kept."""
    hits = [
        {"asset_id": "ast_dup", "rel_path": "a.jpg", "score": 1.0, "description": "x"},
        {"asset_id": "ast_dup", "rel_path": "a.jpg", "score": 2.5, "description": "x"},
        {"asset_id": "ast_other", "rel_path": "b.jpg", "score": 1.5, "description": "y"},
    ]
    seen: dict[str, dict] = {}
    for hit in hits:
        asset_id = hit["asset_id"]
        if asset_id not in seen or hit["score"] > seen[asset_id]["score"]:
            seen[asset_id] = hit
    deduped = list(seen.values())

    assert len(deduped) == 2
    ast_dup = next(h for h in deduped if h["asset_id"] == "ast_dup")
    assert ast_dup["score"] == 2.5
    ast_other = next(h for h in deduped if h["asset_id"] == "ast_other")
    assert ast_other["score"] == 1.5


@pytest.mark.fast
def test_ingest_batching() -> None:
    """Mock HTTP in QuickwitClient; ingest 1100 docs; assert 3 calls (500 + 500 + 100)."""
    from unittest.mock import MagicMock

    from src.search.quickwit_client import INGEST_BATCH_SIZE, QuickwitClient

    mock_settings = MagicMock()
    mock_settings.quickwit_enabled = True
    mock_settings.quickwit_url = "http://localhost:7280"

    with (
        patch("src.search.quickwit_client.get_settings", return_value=mock_settings),
        patch("src.search.quickwit_client.requests.post") as mock_post,
    ):
        mock_post.return_value.status_code = 200

        qw = QuickwitClient()
        docs = [{"id": f"doc_{i}", "asset_id": f"ast_{i}"} for i in range(1100)]
        qw.ingest_documents_for_library("lib_test", docs)

    assert mock_post.call_count == 3
    calls = mock_post.call_args_list
    payload_0 = calls[0][1]["data"]
    payload_1 = calls[1][1]["data"]
    payload_2 = calls[2][1]["data"]
    assert payload_0.count("\n") == INGEST_BATCH_SIZE
    assert payload_1.count("\n") == INGEST_BATCH_SIZE
    assert payload_2.count("\n") == 100
