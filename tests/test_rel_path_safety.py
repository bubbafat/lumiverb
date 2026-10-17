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


# Every C0 control (U+0000-U+001F: a newline, a tab, NUL) and DEL, in a
# file's name and in a folder's.
CONTROL_CHARACTERS = [chr(c) for c in range(0x20)] + ["\x7f"]


@pytest.mark.parametrize("control", CONTROL_CHARACTERS, ids=lambda c: f"U+{ord(c):04X}")
def test_check_rel_path_refuses_a_control_character_in_any_name(control: str) -> None:
    for rel_path in (f"a{control}b.jpg", f"Shoots/a{control}b/c.jpg", f"{control}.jpg", f"a.jpg{control}"):
        with pytest.raises(UnsafeRelPathError):
            check_rel_path(rel_path)


@pytest.mark.parametrize("rel_path", [
    "Shoots/Day 1/A001.mov", "café/résumé.jpg", "🎉 party/🎂.jpg", "日本/写真.jpg", "a b.jpg",
])
def test_check_rel_path_takes_ordinary_unicode_names(rel_path: str) -> None:
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


def test_a_jobs_inputs_never_read_through_a_symlink_out_of_the_root(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from src.processing.originals import Folder
    from src.producers.runner import Failed
    from src.server.scheduler.inputs import Inputs

    root = tmp_path / "library"
    root.mkdir()
    (tmp_path / "x.jpg").write_bytes(b"outside")
    (root / "link.jpg").symlink_to(tmp_path / "x.jpg")
    inputs = Inputs(SimpleNamespace(originals=lambda library_id: Folder(root, library_id)), None)
    with pytest.raises(Failed), inputs.original({"library_id": "lib_1", "rel_path": "link.jpg"}):
        pass
    with inputs.originals([{"asset_id": "ast_1", "library_id": "lib_1", "rel_path": "link.jpg"}]) as found:
        assert found == {}, "quietly left out"


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


def test_api_refuses_a_rel_path_with_a_control_character() -> None:
    from fastapi import HTTPException

    from src.server.api.dependencies import checked_rel_path

    for bad in ("a\nb.jpg", "Shoots\t/a.jpg", "a\x7f.jpg"):
        with pytest.raises(HTTPException) as exc:
            checked_rel_path(bad)
        assert exc.value.status_code == 400


BAD_ROOTS = ["", "/", "Media", "./Media", "~/Media", "/Volumes/media-01/../../etc", "/Volumes/./media-01",
             "/Volumes//media-01", "/Volumes/media-01/", "/Volumes/a\x00b", "\\\\nas\\share", "C:\\Media",
             "/Volumes/a\\..\\b", "/Volumes/a\nb", "/Volumes/media-01/Day\t1", "/Volumes/a\x7fb"]


@pytest.mark.parametrize("root_path", BAD_ROOTS)
def test_api_refuses_library_roots_not_absolute_and_canonical(root_path: str) -> None:
    from fastapi import HTTPException

    from src.server.api.dependencies import checked_root_path

    with pytest.raises(HTTPException) as exc:
        checked_root_path(root_path)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("root_path", ["/Volumes/media-01/Media", "/Users/me/Photos 2024", "/mnt/a..b",
                                       "/Users/me/Photos café 🎉"])
def test_api_takes_absolute_canonical_library_roots(root_path: str) -> None:
    from src.server.api.dependencies import checked_root_path

    assert checked_root_path(root_path) == root_path


def test_the_preview_cache_never_reads_a_librarys_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It has no library root to read from: a preview comes from the cache or the API (ADR-020 decision 4)."""
    import inspect

    from src.processing.proxy import proxy_cache

    monkeypatch.setattr(proxy_cache, "cache_dir", lambda name: tmp_path / "cache" / name)
    assert "root_path" not in inspect.signature(proxy_cache.ProxyCache).parameters
    assert not hasattr(proxy_cache.ProxyCache, "put_from_path")
    assert proxy_cache.ProxyCache().get("ast_x") is None
