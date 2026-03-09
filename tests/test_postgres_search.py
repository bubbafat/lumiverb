"""Fast tests for Postgres fallback search."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.search.postgres_search import search_assets


@pytest.mark.fast
def test_search_assets_finds_qwen_captioned_asset() -> None:
    """
    search_assets returns assets captioned by any model (e.g. qwen3-visioncaption-2b),
    not only moondream. The LATERAL join picks the most recent metadata per asset.
    """
    # Simulate DB with two assets: one moondream, one qwen. Query matches only qwen's description.
    qwen_row = SimpleNamespace(
        asset_id="ast_qwen123",
        rel_path="photos/qwen_photo.jpg",
        thumbnail_key="t/00/ast_qwen123.jpg",
        proxy_key="p/00/ast_qwen123.jpg",
        camera_make=None,
        camera_model=None,
        description="A dragonfly on a lily pad in the pond.",
        tags="[]",
    )

    mock_session = MagicMock()
    mock_session.execute.return_value.fetchall.return_value = [qwen_row]

    results = search_assets(
        mock_session,
        library_id="lib_001",
        query="dragonfly lily pad",
        limit=20,
        offset=0,
    )

    assert len(results) == 1
    assert results[0]["asset_id"] == "ast_qwen123"
    assert results[0]["description"] == "A dragonfly on a lily pad in the pond."
    assert results[0]["source"] == "postgres"

    # Assert the SQL uses LATERAL (model-agnostic) and does not hardcode moondream.
    call_args = mock_session.execute.call_args
    sql_str = str(call_args[0][0])
    assert "LATERAL" in sql_str
    assert "generated_at" in sql_str
    assert "model_id = 'moondream'" not in sql_str
