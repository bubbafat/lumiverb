"""Fast unit tests for --path scoped scanning: missing detection."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.cli.scanner import scan_library


@pytest.mark.fast
def test_missing_outside_path_not_marked(tmp_path: Path) -> None:
    """When path_override is set, assets outside the scanned subtree must be skipped, not marked missing."""
    # Setup: root = tmp_path, path = Photos/HS150/HollyFest2025
    # Create only img001.jpg under the scoped path (local_map will have it)
    subdir = tmp_path / "Photos" / "HS150" / "HollyFest2025"
    subdir.mkdir(parents=True)
    (subdir / "img001.jpg").write_bytes(b"x")

    library = {
        "library_id": "lib_test",
        "root_path": str(tmp_path),
    }
    path_override = "Photos/HS150/HollyFest2025"

    # Server has 3 assets: img001 (in path, local), img002 (in path, missing), img003 (outside path)
    assets_page = [
        {
            "asset_id": "ast_001",
            "rel_path": "Photos/HS150/HollyFest2025/img001.jpg",
            "file_size": 1,
            "file_mtime": "2025-01-01T12:00:00Z",
            "sha256": None,
            "media_type": "image",
        },
        {
            "asset_id": "ast_002",
            "rel_path": "Photos/HS150/HollyFest2025/img002.jpg",
            "file_size": 1,
            "file_mtime": "2025-01-01T12:00:00Z",
            "sha256": None,
            "media_type": "image",
        },
        {
            "asset_id": "ast_003",
            "rel_path": "Photos/OtherFolder/img003.jpg",
            "file_size": 1,
            "file_mtime": "2025-01-01T12:00:00Z",
            "sha256": None,
            "media_type": "image",
        },
    ]

    def _json(d):
        m = MagicMock()
        m.json.return_value = d
        m.status_code = 200
        return m

    batch_calls = []

    def mock_get(path: str, **kwargs):
        m = MagicMock()
        if "/assets/page" in path:
            m.status_code = 200
            m.json.return_value = {"items": assets_page, "next_cursor": None}
        elif "scans/running" in path:
            m.status_code = 200
            m.json.return_value = []
        else:
            m.status_code = 200
            m.json.return_value = []
        return m

    def capture_post(path: str, **kwargs):
        if "/batch" in path:
            batch_calls.append(kwargs.get("json", {}))
        if "/scans" in path and "batch" in path:
            return _json({"added": 0, "updated": 0, "skipped": 0, "missing": 0})
        if "/scans" in path and "complete" in path:
            return _json({
                "scan_id": "scan_1",
                "files_discovered": 3,
                "files_added": 0,
                "files_updated": 0,
                "files_skipped": 0,
                "files_missing": 0,
                "status": "complete",
            })
        if "/scans" in path and "batch" not in path and "complete" not in path:
            return _json({"scan_id": "scan_1"})
        return _json({})

    mock_client = MagicMock()
    mock_client.get.side_effect = mock_get
    mock_client.post.side_effect = capture_post

    with patch("src.cli.scanner.signal.signal"):
        result = scan_library(mock_client, library, path_override=path_override, force=True)

    assert result.status == "complete"

    # Find batch call with items (exclude the add batch which has action "add")
    reconcile_batches = [
        b for b in batch_calls
        if "items" in b and any(i.get("action") in ("skip", "missing", "update") for i in b["items"])
    ]
    assert len(reconcile_batches) >= 1, "expected at least one reconcile batch"
    items = reconcile_batches[0]["items"]

    by_asset = {i["asset_id"]: i for i in items}
    assert "ast_001" in by_asset  # in path, present locally -> skip or update
    assert by_asset["ast_001"]["action"] in ("skip", "update")
    assert "ast_002" in by_asset  # in path, missing locally -> missing
    assert by_asset["ast_002"]["action"] == "missing"
    assert "ast_003" in by_asset  # outside path -> skip (not missing)
    assert by_asset["ast_003"]["action"] == "skip"


