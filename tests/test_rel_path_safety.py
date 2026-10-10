"""A rel_path stays inside its library: one rule, on every write and on disk."""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from src.shared.io_utils import UnsafeRelPathError, check_rel_path, resolve_source_path

pytestmark = pytest.mark.fast

BAD = ["/etc/passwd", "../x.jpg", "a/../../x.jpg", "a/..", "..", "a\x00b.jpg"]


@pytest.mark.parametrize("rel_path", BAD + [""])
def test_check_rel_path_refuses_paths_that_leave_the_library(rel_path: str) -> None:
    with pytest.raises(UnsafeRelPathError):
        check_rel_path(rel_path)


def test_check_rel_path_keeps_ordinary_paths_in_nfc() -> None:
    nfd = unicodedata.normalize("NFD", "café/photo..jpg")
    assert check_rel_path(nfd) == unicodedata.normalize("NFC", nfd)
    assert check_rel_path("", allow_root=True) == ""


@pytest.mark.parametrize("rel_path", BAD)
def test_resolve_source_path_refuses_paths_that_leave_the_root(tmp_path: Path, rel_path: str) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (tmp_path / "x.jpg").write_bytes(b"outside")
    with pytest.raises(UnsafeRelPathError):
        resolve_source_path(root, rel_path)


def test_resolve_source_path_still_finds_the_root_and_files(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.jpg").write_bytes(b"x")
    assert resolve_source_path(tmp_path, "a/b.jpg") == tmp_path / "a" / "b.jpg"
    assert resolve_source_path(tmp_path, "") == tmp_path


def test_api_refuses_unsafe_rel_paths() -> None:
    from fastapi import HTTPException

    from src.server.api.dependencies import checked_rel_path

    for bad in BAD:
        with pytest.raises(HTTPException) as exc:
            checked_rel_path(bad)
        assert exc.value.status_code == 400
