"""Scanner path filter integration: unit tests with mocked filesystem."""

from __future__ import annotations

import pytest
from pathlib import Path

from src.cli.scanner import _build_local_map
from src.core.path_filter import PathFilter


@pytest.mark.fast
def test_scanner_empty_filter_list_all_paths_included(tmp_path: Path) -> None:
    """Empty filter list: all supported files appear in local map."""
    (tmp_path / "Photos").mkdir()
    (tmp_path / "Photos" / "IMG_001.jpg").write_bytes(b"x")
    (tmp_path / "Videos").mkdir()
    (tmp_path / "Videos" / "clip.mov").write_bytes(b"y")
    (tmp_path / "Photos" / "Proxy").mkdir()
    (tmp_path / "Photos" / "Proxy" / "p.mov").write_bytes(b"z")

    local_map = _build_local_map(tmp_path, tmp_path, path_filters=None)
    assert len(local_map) == 3
    assert "Photos/IMG_001.jpg" in local_map
    assert "Videos/clip.mov" in local_map
    assert "Photos/Proxy/p.mov" in local_map


@pytest.mark.fast
def test_scanner_skips_paths_that_fail_is_path_included(tmp_path: Path) -> None:
    """Scanner (via _build_local_map) skips paths that fail is_path_included."""
    (tmp_path / "Photos").mkdir()
    (tmp_path / "Photos" / "IMG_001.jpg").write_bytes(b"x")
    (tmp_path / "Photos" / "Proxy").mkdir()
    (tmp_path / "Photos" / "Proxy" / "p.mov").write_bytes(b"y")
    (tmp_path / "Originals").mkdir()
    (tmp_path / "Originals" / "o.jpg").write_bytes(b"z")

    filters = [
        PathFilter(type="include", pattern="Photos/**"),
        PathFilter(type="exclude", pattern="**/Proxy/**"),
    ]
    local_map = _build_local_map(tmp_path, tmp_path, path_filters=filters)
    assert "Photos/IMG_001.jpg" in local_map
    assert "Photos/Proxy/p.mov" not in local_map
    assert "Originals/o.jpg" not in local_map
    assert len(local_map) == 1


@pytest.mark.fast
def test_scanner_ingests_paths_that_pass_is_path_included(tmp_path: Path) -> None:
    """Scanner ingests paths that pass is_path_included."""
    (tmp_path / "Photos").mkdir()
    (tmp_path / "Photos" / "2024").mkdir()
    (tmp_path / "Photos" / "2024" / "a.jpg").write_bytes(b"a")
    (tmp_path / "Photos" / "2024" / "b.mov").write_bytes(b"b")

    filters = [PathFilter(type="include", pattern="Photos/**")]
    local_map = _build_local_map(tmp_path, tmp_path, path_filters=filters)
    assert "Photos/2024/a.jpg" in local_map
    assert "Photos/2024/b.mov" in local_map
    assert len(local_map) == 2
