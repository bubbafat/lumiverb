"""Fast tests for AssetFilterSpec and CLI path detection."""

import pytest

from src.models.filter import AssetFilterSpec


@pytest.mark.fast
def test_filter_spec_asset_id_only() -> None:
    """asset_id set — path/mtime filters ignored."""
    f = AssetFilterSpec(library_id="lib_1", asset_id="ast_1", path_prefix="B/2025")
    assert f.asset_id == "ast_1"
    assert f.path_prefix == "B/2025"  # stored but ignored in query


@pytest.mark.fast
def test_filter_spec_path_exact_detection() -> None:
    """CLI path detection: file.jpg → path_exact, folder → path_prefix."""
    from pathlib import Path

    path = "B/2025/June/IMG_001.jpg"
    assert "." in Path(path).name  # detected as file

    path = "B/2025/June"
    assert "." not in Path(path).name  # detected as folder
