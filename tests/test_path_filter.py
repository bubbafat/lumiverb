"""Unit tests for path filter evaluation. Pure logic, no DB."""

from __future__ import annotations

import pytest

from src.core.path_filter import PathFilter, is_path_included, validate_pattern


@pytest.mark.fast
def test_no_filters_all_included() -> None:
    """No filters → all paths included."""
    assert is_path_included("", []) is True
    assert is_path_included("Photos/IMG_001.jpg", []) is True
    assert is_path_included("any/path/here.mov", []) is True


@pytest.mark.fast
def test_include_filter_only_matching() -> None:
    """Include filter → only matching paths included."""
    filters = [PathFilter(type="include", pattern="Photos/**")]
    assert is_path_included("Photos/IMG_001.jpg", filters) is True
    assert is_path_included("Photos/2024/IMG_002.jpg", filters) is True
    assert is_path_included("Videos/clip.mov", filters) is False
    assert is_path_included("root.jpg", filters) is False


@pytest.mark.fast
def test_exclude_filter_matching_excluded() -> None:
    """Exclude filter → matching paths excluded."""
    filters = [PathFilter(type="exclude", pattern="**/Proxy/**")]
    assert is_path_included("Photos/Proxy/clip.mov", filters) is False
    assert is_path_included("a/b/Proxy/c.mov", filters) is False
    assert is_path_included("Photos/Originals/clip.mov", filters) is True
    assert is_path_included("Proxy/root.mov", filters) is False


@pytest.mark.fast
def test_include_plus_exclude_include_scoped_then_exclude_pruned() -> None:
    """Include + exclude: include scoped, then exclude pruned within that set."""
    filters = [
        PathFilter(type="include", pattern="Photos/**"),
        PathFilter(type="exclude", pattern="**/Proxy/**"),
    ]
    assert is_path_included("Photos/Originals/IMG.jpg", filters) is True
    assert is_path_included("Photos/Proxy/IMG.jpg", filters) is False
    assert is_path_included("Videos/clip.mov", filters) is False


@pytest.mark.fast
def test_double_star_proxy_excludes_any_depth() -> None:
    """**/Proxy/** excludes any path containing a Proxy directory at any depth."""
    filters = [PathFilter(type="exclude", pattern="**/Proxy/**")]
    assert is_path_included("Photos/Proxy/clip.mov", filters) is False
    assert is_path_included("Project/Media/Proxy/foo.mov", filters) is False
    assert is_path_included("Proxy/file.mov", filters) is False
    assert is_path_included("Photos/Originals/file.mov", filters) is True


@pytest.mark.fast
def test_star_mov_excludes_all_mov_regardless_of_depth() -> None:
    """*.mov excludes all MOV files (single segment)."""
    filters = [PathFilter(type="exclude", pattern="*.mov")]
    assert is_path_included("clip.mov", filters) is False
    assert is_path_included("Photos/clip.mov", filters) is True  # *.mov is one segment only
    # **/*.mov would match at any depth
    filters2 = [PathFilter(type="exclude", pattern="**/*.mov")]
    assert is_path_included("clip.mov", filters2) is False
    assert is_path_included("Photos/clip.mov", filters2) is False


@pytest.mark.fast
def test_case_insensitive_proxy_matches_Photos_Proxy() -> None:
    """Case-insensitivity: **/proxy/** matches Photos/Proxy/clip.mov."""
    filters = [PathFilter(type="exclude", pattern="**/proxy/**")]
    assert is_path_included("Photos/Proxy/clip.mov", filters) is False
    assert is_path_included("PHOTOS/PROXY/CLIP.MOV", filters) is False


@pytest.mark.fast
def test_validate_pattern_rejects_dot_dot() -> None:
    """validate_pattern rejects ../etc/passwd."""
    with pytest.raises(ValueError, match=r"\.\.|'\.\.'"):
        validate_pattern("../etc/passwd")
    with pytest.raises(ValueError):
        validate_pattern("Photos/../secret")


@pytest.mark.fast
def test_validate_pattern_rejects_null_bytes() -> None:
    """validate_pattern rejects patterns with null bytes."""
    with pytest.raises(ValueError, match="null"):
        validate_pattern("Photos/\x00secret")


@pytest.mark.fast
def test_validate_pattern_accepts_valid() -> None:
    """validate_pattern returns pattern unchanged for valid patterns."""
    assert validate_pattern("Photos/**") == "Photos/**"
    assert validate_pattern("**/*.mov") == "**/*.mov"
    assert validate_pattern("*.jpg") == "*.jpg"
