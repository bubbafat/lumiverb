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


# Not their one form: "./private/a.jpg" would pass a "private/**" exclude.
NOT_CANONICAL = ["./a.jpg", "a//b.jpg", "a/./b.jpg", "a/", "a/b/.", "a\\b.jpg", "C:x.jpg", "c:/x.jpg"]


@pytest.mark.parametrize("rel_path", NOT_CANONICAL)
def test_check_rel_path_refuses_paths_not_in_their_one_form(rel_path: str) -> None:
    with pytest.raises(UnsafeRelPathError):
        check_rel_path(rel_path)


@pytest.mark.parametrize("rel_path", ["a.jpg", "a/b.jpg", ".hid/g.jpg", "a..b/c.jpg", "C/x.jpg"])
def test_check_rel_path_takes_canonical_paths(rel_path: str) -> None:
    assert check_rel_path(rel_path) == rel_path


def test_resolve_source_path_refuses_a_symlink_out_of_the_root(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "x.jpg").write_bytes(b"outside")
    (root / "link.jpg").symlink_to(tmp_path / "other" / "x.jpg")
    (root / "linked").symlink_to(tmp_path / "other")
    for rel_path in ("link.jpg", "linked/x.jpg", "linked"):
        with pytest.raises(UnsafeRelPathError):
            resolve_source_path(root, rel_path)


def test_resolve_source_path_follows_a_symlink_inside_the_root(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.jpg").write_bytes(b"x")
    (tmp_path / "same").symlink_to(tmp_path / "a")
    assert resolve_source_path(tmp_path, "same/b.jpg") == tmp_path / "same" / "b.jpg"


def test_the_runner_never_reads_through_a_symlink_out_of_the_root(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from src.producers.runner import Failed, Work

    root = tmp_path / "library"
    root.mkdir()
    (tmp_path / "x.jpg").write_bytes(b"outside")
    (root / "link.jpg").symlink_to(tmp_path / "x.jpg")
    work = Work.__new__(Work)
    work.acct = SimpleNamespace(root=lambda _library_id: root)
    with pytest.raises(Failed):
        work.original({"library_id": "lib_1", "rel_path": "link.jpg"})


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


def test_api_refuses_rel_paths_not_in_their_one_form() -> None:
    from fastapi import HTTPException

    from src.server.api.dependencies import checked_rel_path

    for bad in NOT_CANONICAL:
        with pytest.raises(HTTPException) as exc:
            checked_rel_path(bad)
        assert exc.value.status_code == 400


BAD_ROOTS = ["", "/", "Media", "./Media", "~/Media", "/Volumes/media-01/../../etc", "/Volumes/./media-01",
             "/Volumes//media-01", "/Volumes/media-01/", "/Volumes/a\x00b", "\\\\nas\\share", "C:\\Media",
             "/Volumes/a\\..\\b"]


@pytest.mark.parametrize("root_path", BAD_ROOTS)
def test_api_refuses_library_roots_not_absolute_and_canonical(root_path: str) -> None:
    from fastapi import HTTPException

    from src.server.api.dependencies import checked_root_path

    with pytest.raises(HTTPException) as exc:
        checked_root_path(root_path)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("root_path", ["/Volumes/media-01/Media", "/Users/me/Photos 2024", "/mnt/a..b"])
def test_api_takes_absolute_canonical_library_roots(root_path: str) -> None:
    from src.server.api.dependencies import checked_root_path

    assert checked_root_path(root_path) == root_path


def test_proxy_cache_never_reads_outside_the_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch

    from src.processing.proxy import proxy_cache

    monkeypatch.setattr(proxy_cache, "cache_dir", lambda name: tmp_path / "cache" / name)
    root = tmp_path / "library"
    root.mkdir()
    (tmp_path / "secret.jpg").write_bytes(b"outside")
    cache = proxy_cache.ProxyCache(root_path=root)
    with patch.object(cache, "put_from_path") as put:
        assert cache.get("ast_x", "../secret.jpg") is None
    put.assert_not_called()
