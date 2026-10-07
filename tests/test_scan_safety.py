"""Scanning is safe from a network mount (ADR-016 phase 0).

- rel_path is Unicode NFC, as the macOS scanner stores it, so the same file
  has one identity whichever machine scans it.
- Source files are found on disk even when their names are stored in
  another normalization form (macOS-created NFD names on a Linux mount).
- A scan that would delete a large share of the library skips deletions,
  as the macOS scanner does: the root is probably half-mounted.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.client.cli.ingest import _walk_library
from src.client.cli.scan import _ServerAsset, _deletion_guard_trips, run_scan
from src.shared.io_utils import resolve_source_path

NFC = unicodedata.normalize("NFC", "Café/résumé.jpg")
NFD = unicodedata.normalize("NFD", NFC)


def _write(root: Path, rel: str, data: bytes = b"jpeg") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.fast
def test_walk_library_stores_nfc_rel_path(tmp_path: Path) -> None:
    _write(tmp_path, NFD)

    [entry] = _walk_library(tmp_path)

    assert entry["rel_path"] == NFC
    assert entry["rel_path"] != NFD


@pytest.mark.fast
@pytest.mark.parametrize(
    "on_disk",
    [
        NFD,
        unicodedata.normalize("NFC", "Café") + "/" + unicodedata.normalize("NFD", "résumé.jpg"),
        unicodedata.normalize("NFD", "Café") + "/" + unicodedata.normalize("NFC", "résumé.jpg"),
    ],
)
def test_resolve_source_path_finds_other_normalization(tmp_path: Path, on_disk: str) -> None:
    _write(tmp_path, on_disk, b"original bytes")

    path = resolve_source_path(tmp_path, NFC)

    assert path.read_bytes() == b"original bytes"


@pytest.mark.fast
def test_resolve_source_path_plain_hit_and_miss(tmp_path: Path) -> None:
    _write(tmp_path, "a/b.jpg")

    assert resolve_source_path(tmp_path, "a/b.jpg") == tmp_path / "a/b.jpg"
    assert resolve_source_path(tmp_path, "a/missing.jpg") == tmp_path / "a/missing.jpg"
    assert resolve_source_path(tmp_path, "nope/x.jpg") == tmp_path / "nope/x.jpg"


@pytest.mark.fast
@pytest.mark.parametrize(
    ("deleting", "on_server", "trips"),
    [
        (50, 100, False),  # 50 or fewer files always proceed
        (60, 10_000, False),  # 0.6% of the library
        (60, 100, True),
        (600, 10_000, True),
        (0, 0, False),
    ],
)
def test_deletion_guard_matches_macos_threshold(deleting: int, on_server: int, trips: bool) -> None:
    assert _deletion_guard_trips(deleting, on_server) is trips


def _scan_with(tmp_path: Path, *, on_disk: int, on_server: int, **kwargs) -> MagicMock:
    """run_scan with `on_server` assets on the server, of which `on_disk` are on disk unchanged."""
    root = tmp_path / "lib"
    root.mkdir()
    local = [
        {"rel_path": f"f{i}.jpg", "file_size": 4, "file_mtime": None, "media_type": "image", "ext": ".jpg"}
        for i in range(on_disk)
    ]
    existing = {
        f"f{i}.jpg": _ServerAsset(asset_id=f"ast_{i}", sha256="x", file_size=4) for i in range(on_server)
    }
    client = MagicMock()
    with (
        patch("src.client.cli.scan._load_tenant_filters", return_value=[]),
        patch("src.client.cli.scan._load_library_filters", return_value=[]),
        patch("src.client.cli.scan._walk_library", return_value=local),
        patch("src.client.cli.scan._fetch_existing_assets_with_sha", return_value=existing),
        patch("src.client.cli.scan._fetch_ignored_paths", return_value=set()),
        patch("src.client.cli.scan._split_files", return_value=([], [], local)),
        patch("src.client.cli.scan._populate_cache_for_unchanged"),
    ):
        run_scan(
            client,
            {"library_id": "lib_1", "root_path": str(root)},
            console=Console(quiet=True),
            skip_moves=False,
            allow_moves=True,
            **kwargs,
        )
    return client


def _deleted_ids(client: MagicMock) -> list[str]:
    ids: list[str] = []
    for call in client.delete.call_args_list:
        if call.args and call.args[0] == "/v1/assets":
            ids.extend(call.kwargs["json"]["asset_ids"])
    return ids


@pytest.mark.fast
def test_scan_skips_mass_deletion(tmp_path: Path) -> None:
    client = _scan_with(tmp_path, on_disk=10, on_server=100)

    assert _deleted_ids(client) == []


@pytest.mark.fast
def test_scan_mass_deletion_can_be_allowed(tmp_path: Path) -> None:
    client = _scan_with(tmp_path, on_disk=10, on_server=100, allow_mass_delete=True)

    assert len(_deleted_ids(client)) == 90


@pytest.mark.fast
def test_scan_small_deletion_proceeds(tmp_path: Path) -> None:
    client = _scan_with(tmp_path, on_disk=95, on_server=100)

    assert len(_deleted_ids(client)) == 5
