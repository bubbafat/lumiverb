"""Tests for local storage abstraction."""

import re
from pathlib import Path

import pytest
from ulid import ULID

from src.server.storage.local import LocalStorage


@pytest.mark.fast
def test_proxy_key_format() -> None:
    """Assert proxy key matches expected pattern and bucket is 2 digits."""
    storage = LocalStorage(data_dir="/data")
    tenant_id = "ten_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    library_id = "lib_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    asset_id = "ast_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    original_filename = "my_photo.jpg"

    key = storage.proxy_key(tenant_id, library_id, asset_id, original_filename)

    # Pattern: tenant_id/library_id/proxies/{2-digit}/{asset_id}_{stem}.webp
    pattern = re.compile(
        r"^[^/]+/[^/]+/proxies/\d{2}/ast_[A-Z0-9]+_my_photo\.webp$"
    )
    assert pattern.match(key), f"Key {key!r} did not match expected pattern"
    parts = key.split("/")
    assert parts[0] == tenant_id
    assert parts[1] == library_id
    assert parts[2] == "proxies"
    assert len(parts[3]) == 2 and parts[3].isdigit()
    assert parts[4].startswith("ast_") and parts[4].endswith("_my_photo.webp")


@pytest.mark.fast
def test_thumbnail_key_format() -> None:
    """Assert thumbnail key matches expected pattern and bucket is 2 digits."""
    storage = LocalStorage(data_dir="/data")
    tenant_id = "ten_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    library_id = "lib_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    asset_id = "ast_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    original_filename = "vacation.png"

    key = storage.thumbnail_key(tenant_id, library_id, asset_id, original_filename)

    pattern = re.compile(
        r"^[^/]+/[^/]+/thumbnails/\d{2}/ast_[A-Z0-9]+_vacation\.webp$"
    )
    assert pattern.match(key), f"Key {key!r} did not match expected pattern"
    parts = key.split("/")
    assert parts[2] == "thumbnails"
    assert len(parts[3]) == 2 and parts[3].isdigit()


@pytest.mark.fast
def test_scene_rep_key_format() -> None:
    """Assert scene rep key uses scenes/ bucket and zero-padded rep_frame_ms."""
    storage = LocalStorage(data_dir="/data")
    tenant_id = "ten_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    library_id = "lib_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    asset_id = "ast_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    rep_frame_ms = 12345

    key = storage.scene_rep_key(tenant_id, library_id, asset_id, rep_frame_ms)

    # Pattern: tenant_id/library_id/scenes/{2-digit}/{asset_id}_{rep_frame_ms:010d}.jpg
    pattern = re.compile(
        r"^[^/]+/[^/]+/scenes/\d{2}/ast_[A-Z0-9]+_\d{10}\.jpg$"
    )
    assert pattern.match(key), f"Key {key!r} did not match expected pattern"
    parts = key.split("/")
    assert parts[2] == "scenes"
    assert parts[4].endswith("_0000012345.jpg")


@pytest.mark.fast
def test_write_and_exists(tmp_path: Path) -> None:
    """Write bytes, assert exists returns True."""
    storage = LocalStorage(data_dir=str(tmp_path))
    key = "tenant/lib/proxies/42/ast_01ARZ3NDEKTSV4RRFFQ69G5FAV_photo.jpg"

    assert storage.exists(key) is False
    storage.write(key, b"jpeg content")
    assert storage.exists(key) is True
    assert storage.abs_path(key).read_bytes() == b"jpeg content"


@pytest.mark.fast
def test_write_is_atomic(tmp_path: Path) -> None:
    """Verify .tmp file does not exist after write."""
    storage = LocalStorage(data_dir=str(tmp_path))
    key = "tenant/lib/proxies/00/ast_01ARZ3NDEKTSV4RRFFQ69G5FAV_photo.jpg"
    path = storage.abs_path(key)
    tmp_path_file = path.with_name(path.name + ".tmp")

    storage.write(key, b"data")
    assert path.exists()
    assert not tmp_path_file.exists()


@pytest.mark.fast
def test_bucket_distribution() -> None:
    """Generate 100 asset_ids, assert buckets 0-99 all present."""
    storage = LocalStorage(data_dir="/data")
    buckets: set[int] = set()
    # Generate enough asset_ids to likely cover all 100 buckets
    for _ in range(500):
        asset_id = "ast_" + str(ULID())
        key = storage.proxy_key("ten_1", "lib_1", asset_id, "x.jpg")
        parts = key.split("/")
        bucket = int(parts[3])
        buckets.add(bucket)
        if len(buckets) == 100:
            break

    assert len(buckets) == 100, f"Got buckets {sorted(buckets)}"
    assert buckets == set(range(100))
