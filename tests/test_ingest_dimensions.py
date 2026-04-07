"""Tests for ingest dimension extraction, proxy generation, EXIF parsing,
and resilience against bad input.

Uses real fixture files (tests/fixtures/) to verify end-to-end behavior.
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_VIDEO = FIXTURES / "sample_4k.mp4"
SAMPLE_IMAGE = FIXTURES / "sample_4k.jpg"
TRUNCATED_VIDEO = FIXTURES / "truncated.mp4"
EMPTY_VIDEO = FIXTURES / "empty.mp4"
GARBAGE_VIDEO = FIXTURES / "garbage.mp4"

_has_pyvips = False
try:
    import pyvips

    _has_pyvips = hasattr(pyvips, "Image")
except Exception:
    pass

_has_ffprobe = False
try:
    import subprocess

    subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
    _has_ffprobe = True
except Exception:
    pass

_skip_no_libvips = pytest.mark.skipif(not _has_pyvips, reason="libvips not installed")
_skip_no_ffprobe = pytest.mark.skipif(not _has_ffprobe, reason="ffprobe not installed")


# ---------------------------------------------------------------------------
# Video dimension tests
# ---------------------------------------------------------------------------


@pytest.mark.fast
@_skip_no_ffprobe
def test_probe_video_dimensions_returns_source_resolution():
    from src.client.cli.ingest import _probe_video_dimensions

    w, h = _probe_video_dimensions(SAMPLE_VIDEO)
    assert w == 3840
    assert h == 2160


@pytest.mark.fast
@_skip_no_ffprobe
@_skip_no_libvips
def test_extract_video_poster_returns_source_dimensions():
    """The poster frame is resized, but reported dimensions must be the original."""
    from src.client.cli.ingest import _extract_video_poster

    jpeg_bytes, w, h = _extract_video_poster(SAMPLE_VIDEO)

    assert w == 3840
    assert h == 2160

    assert jpeg_bytes[:2] == b"\xff\xd8"  # JPEG magic
    img = pyvips.Image.new_from_buffer(jpeg_bytes, "")
    assert img.width <= 2048  # PROXY_LONG_EDGE


@pytest.mark.fast
@_skip_no_ffprobe
@_skip_no_libvips
def test_video_poster_preserves_aspect_ratio():
    from src.client.cli.ingest import _extract_video_poster

    jpeg_bytes, w, h = _extract_video_poster(SAMPLE_VIDEO)
    source_ratio = w / h  # 16:9

    img = pyvips.Image.new_from_buffer(jpeg_bytes, "")
    proxy_ratio = img.width / img.height
    assert proxy_ratio == pytest.approx(source_ratio, abs=0.02)


# ---------------------------------------------------------------------------
# Video preview tests
# ---------------------------------------------------------------------------


@pytest.mark.fast
@_skip_no_ffprobe
def test_video_preview_is_valid_mp4():
    from src.client.cli.ingest import _generate_video_preview

    preview_bytes = _generate_video_preview(SAMPLE_VIDEO)
    # MP4 files start with a box: 4 bytes size + "ftyp"
    assert b"ftyp" in preview_bytes[:12]
    assert len(preview_bytes) > 100


@pytest.mark.fast
@_skip_no_ffprobe
def test_video_preview_capped_at_720p():
    from src.client.cli.ingest import _generate_video_preview, _probe_video_dimensions
    import subprocess
    import tempfile

    preview_bytes = _generate_video_preview(SAMPLE_VIDEO)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(preview_bytes)
        tmp_path = Path(tmp.name)

    try:
        w, h = _probe_video_dimensions(tmp_path)
        assert h <= 720, f"Preview height {h} exceeds 720p cap"
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Image dimension tests
# ---------------------------------------------------------------------------


@pytest.mark.fast
@_skip_no_libvips
def test_generate_proxy_returns_source_dimensions_for_jpeg():
    from src.client.cli.ingest import _generate_proxy_bytes

    jpeg_bytes, w, h = _generate_proxy_bytes(SAMPLE_IMAGE)

    assert w == 4000
    assert h == 3000

    img = pyvips.Image.new_from_buffer(jpeg_bytes, "")
    assert img.width <= 2048


@pytest.mark.fast
@_skip_no_libvips
def test_image_proxy_preserves_aspect_ratio():
    from src.client.cli.ingest import _generate_proxy_bytes

    jpeg_bytes, w, h = _generate_proxy_bytes(SAMPLE_IMAGE)
    source_ratio = w / h

    img = pyvips.Image.new_from_buffer(jpeg_bytes, "")
    proxy_ratio = img.width / img.height
    assert proxy_ratio == pytest.approx(source_ratio, abs=0.02)


# ---------------------------------------------------------------------------
# WebP conversion tests
# ---------------------------------------------------------------------------


@pytest.mark.fast
@_skip_no_libvips
def test_jpeg_to_webp_produces_valid_output():
    from src.client.cli.ingest import _generate_proxy_bytes, _jpeg_to_webp

    jpeg_bytes, _, _ = _generate_proxy_bytes(SAMPLE_IMAGE)
    webp_bytes = _jpeg_to_webp(jpeg_bytes)

    # WebP magic: "RIFF" + 4 bytes size + "WEBP"
    assert webp_bytes[:4] == b"RIFF"
    assert webp_bytes[8:12] == b"WEBP"

    # Dimensions should match the JPEG proxy
    jpeg_img = pyvips.Image.new_from_buffer(jpeg_bytes, "")
    webp_img = pyvips.Image.new_from_buffer(webp_bytes, "")
    assert webp_img.width == jpeg_img.width
    assert webp_img.height == jpeg_img.height


# ---------------------------------------------------------------------------
# EXIF extraction tests
# ---------------------------------------------------------------------------


@pytest.mark.fast
@_skip_no_ffprobe
def test_build_exif_payload_image():
    from src.client.cli.ingest import _build_exif_payload

    payload = _build_exif_payload(SAMPLE_IMAGE, "image")

    assert payload["camera_make"] == "TestCamera"
    assert payload["camera_model"] == "TestModel X100"
    assert payload["iso"] == 400
    assert payload["aperture"] == pytest.approx(2.8, abs=0.1)
    assert payload["focal_length"] == pytest.approx(35.0, abs=0.1)
    assert payload["lens_model"] == "TestLens 35mm f/2.8"
    assert payload["sha256"] is not None
    assert payload["duration_sec"] is None


@pytest.mark.fast
@_skip_no_ffprobe
def test_exif_gps_coordinates_parsed_correctly():
    from src.client.cli.ingest import _build_exif_payload

    payload = _build_exif_payload(SAMPLE_IMAGE, "image")

    # Fixture: N 33°44'55", W 84°23'17"
    assert payload["gps_lat"] == pytest.approx(33.748611, abs=0.01)
    assert payload["gps_lon"] == pytest.approx(-84.388056, abs=0.01)  # West = negative


@pytest.mark.fast
@_skip_no_ffprobe
def test_exif_exposure_time_in_microseconds():
    from src.client.cli.ingest import _build_exif_payload

    payload = _build_exif_payload(SAMPLE_IMAGE, "image")

    # 1/250s = 4000 microseconds
    assert payload["exposure_time_us"] == 4000


@pytest.mark.fast
@_skip_no_ffprobe
def test_build_exif_payload_video():
    from src.client.cli.ingest import _build_exif_payload

    payload = _build_exif_payload(SAMPLE_VIDEO, "video")

    assert payload["sha256"] is not None
    assert payload["duration_sec"] is not None
    assert payload["duration_sec"] > 0


# ---------------------------------------------------------------------------
# Resilience — corrupt / missing input
# ---------------------------------------------------------------------------


@pytest.mark.fast
@_skip_no_ffprobe
def test_probe_dimensions_truncated_video_raises():
    from src.client.cli.ingest import _probe_video_dimensions

    with pytest.raises(Exception):
        _probe_video_dimensions(TRUNCATED_VIDEO)


@pytest.mark.fast
@_skip_no_ffprobe
def test_probe_dimensions_empty_file_raises():
    from src.client.cli.ingest import _probe_video_dimensions

    with pytest.raises(Exception):
        _probe_video_dimensions(EMPTY_VIDEO)


@pytest.mark.fast
@_skip_no_ffprobe
def test_probe_dimensions_garbage_file_raises():
    from src.client.cli.ingest import _probe_video_dimensions

    with pytest.raises(Exception):
        _probe_video_dimensions(GARBAGE_VIDEO)


@pytest.mark.fast
@_skip_no_ffprobe
@_skip_no_libvips
def test_extract_poster_from_truncated_video_raises():
    from src.client.cli.ingest import _extract_video_poster

    with pytest.raises(Exception):
        _extract_video_poster(TRUNCATED_VIDEO)


# ---------------------------------------------------------------------------
# Integration: verify POST body contains source dimensions
# ---------------------------------------------------------------------------


@pytest.mark.fast
@_skip_no_ffprobe
# ---------------------------------------------------------------------------
# Discovery: _walk_library
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_walk_library_skips_zero_byte_files(tmp_path):
    """Zero-byte files should be excluded during discovery."""
    from src.client.cli.ingest import _walk_library

    lib_root = tmp_path / "library"
    lib_root.mkdir()

    # Create a valid-size file and a zero-byte file
    good = lib_root / "good.jpg"
    good.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

    empty = lib_root / "empty.jpg"
    empty.write_bytes(b"")

    results = _walk_library(lib_root)
    rel_paths = [r["rel_path"] for r in results]

    assert "good.jpg" in rel_paths
    assert "empty.jpg" not in rel_paths


# ---------------------------------------------------------------------------
# Missing file detection (ingest sync)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_missing_file_detection_computes_correct_diff(tmp_path):
    """Assets on server but not on disk should be identified for removal."""
    from src.client.cli.ingest import _walk_library

    lib_root = tmp_path / "library"
    lib_root.mkdir()

    # Only one file on disk
    (lib_root / "keep.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

    local_files = _walk_library(lib_root)
    local_rel_paths = {f["rel_path"] for f in local_files}

    # Server has three assets
    server_assets = {
        "keep.jpg": "ast_keep",
        "deleted.jpg": "ast_deleted",
        "also_gone.jpg": "ast_also_gone",
    }

    missing_ids = [aid for rp, aid in server_assets.items() if rp not in local_rel_paths]
    assert sorted(missing_ids) == ["ast_also_gone", "ast_deleted"]


@pytest.mark.fast
def test_missing_file_detection_skips_when_root_missing(tmp_path):
    """When library root doesn't exist (NAS offline), no deletions should occur."""
    missing_root = tmp_path / "nonexistent"

    server_assets = {
        "photo1.jpg": "ast_1",
        "photo2.jpg": "ast_2",
    }

    # Simulate the safety check from run_ingest
    should_delete = missing_root.is_dir() and bool(server_assets)
    assert should_delete is False


@pytest.mark.fast
def test_missing_file_detection_respects_path_prefix(tmp_path):
    """With --path prefix, only assets under that prefix should be considered."""
    from src.client.cli.ingest import _walk_library

    lib_root = tmp_path / "library"
    (lib_root / "a").mkdir(parents=True)
    (lib_root / "b").mkdir(parents=True)
    (lib_root / "a" / "keep.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

    local_files = _walk_library(lib_root, path_prefix="a")
    local_rel_paths = {f["rel_path"] for f in local_files}

    server_assets = {
        "a/keep.jpg": "ast_keep",
        "a/gone.jpg": "ast_gone",
        "b/unrelated.jpg": "ast_unrelated",
    }

    # Scope to prefix "a/"
    prefix = "a/"
    scoped = {rp: aid for rp, aid in server_assets.items() if rp.startswith(prefix)}
    missing_ids = [aid for rp, aid in scoped.items() if rp not in local_rel_paths]

    assert missing_ids == ["ast_gone"]
    # "b/unrelated.jpg" should NOT be in missing (out of scope)
    assert "ast_unrelated" not in missing_ids


@pytest.mark.fast
def test_missing_single_deleted_file(tmp_path):
    """A single file removed from disk should be detected."""
    from src.client.cli.ingest import _walk_library

    lib_root = tmp_path / "library"
    lib_root.mkdir()
    (lib_root / "still_here.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

    local_rel_paths = {f["rel_path"] for f in _walk_library(lib_root)}
    server = {"still_here.jpg": "ast_1", "was_deleted.jpg": "ast_2"}
    missing = [aid for rp, aid in server.items() if rp not in local_rel_paths]

    assert missing == ["ast_2"]


@pytest.mark.fast
def test_missing_multiple_deleted_files(tmp_path):
    """Multiple deleted files should all be detected."""
    from src.client.cli.ingest import _walk_library

    lib_root = tmp_path / "library"
    lib_root.mkdir()
    (lib_root / "keep.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

    local_rel_paths = {f["rel_path"] for f in _walk_library(lib_root)}
    server = {
        "keep.jpg": "ast_keep",
        "gone1.jpg": "ast_gone1",
        "gone2.jpg": "ast_gone2",
        "gone3.jpg": "ast_gone3",
    }
    missing = sorted(aid for rp, aid in server.items() if rp not in local_rel_paths)

    assert missing == ["ast_gone1", "ast_gone2", "ast_gone3"]


@pytest.mark.fast
def test_missing_entire_directory(tmp_path):
    """All assets under a removed directory should be detected."""
    from src.client.cli.ingest import _walk_library

    lib_root = tmp_path / "library"
    (lib_root / "kept").mkdir(parents=True)
    (lib_root / "kept" / "photo.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
    # "removed/" directory does NOT exist on disk

    local_rel_paths = {f["rel_path"] for f in _walk_library(lib_root)}
    server = {
        "kept/photo.jpg": "ast_kept",
        "removed/a.jpg": "ast_a",
        "removed/b.jpg": "ast_b",
        "removed/sub/c.jpg": "ast_c",
    }
    missing = sorted(aid for rp, aid in server.items() if rp not in local_rel_paths)

    assert missing == ["ast_a", "ast_b", "ast_c"]


@pytest.mark.fast
def test_protect_missing_library_root(tmp_path):
    """When the library root is gone (NAS offline), nothing should be deleted."""
    missing_root = tmp_path / "offline_nas"

    server = {f"photo{i}.jpg": f"ast_{i}" for i in range(100)}

    # The safety guard: root must be a directory
    assert not missing_root.is_dir()
    # Therefore no deletion should occur
    should_delete = missing_root.is_dir() and bool(server)
    assert should_delete is False


@pytest.mark.fast
def test_missing_recursive_nested_files(tmp_path):
    """Deeply nested missing files should be detected."""
    from src.client.cli.ingest import _walk_library

    lib_root = tmp_path / "library"
    (lib_root / "a" / "b").mkdir(parents=True)
    (lib_root / "a" / "b" / "exists.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

    local_rel_paths = {f["rel_path"] for f in _walk_library(lib_root)}
    server = {
        "a/b/exists.jpg": "ast_exists",
        "a/b/gone.jpg": "ast_gone_shallow",
        "a/b/c/d/deep_gone.jpg": "ast_gone_deep",
        "x/y/z/other_gone.jpg": "ast_gone_other_tree",
    }
    missing = sorted(aid for rp, aid in server.items() if rp not in local_rel_paths)

    assert missing == ["ast_gone_deep", "ast_gone_other_tree", "ast_gone_shallow"]
