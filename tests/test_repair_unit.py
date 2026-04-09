"""Unit tests for ``src/client/cli/repair.py``.

These mock the LumiverbClient HTTP layer and exercise the orchestration
helpers (``_page_missing``, ``_page_all_images``, ``_repair_embed_one``,
``_ocr_one``, ``_transcribe_one``, ``_RepairStats``, ``get_repair_summary``)
and the high-level ``run_repair`` plan-builder via ``dry_run=True``.

Anything that touches a real GPU model, ffmpeg, or whisper subprocess is
mocked at its module entry point — these tests must remain in the ``fast``
marker bucket.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.client.cli import repair as repair_mod
from src.client.cli.repair import (
    _RepairStats,
    _ocr_one,
    _page_all_images,
    _page_missing,
    _repair_embed_one,
    _transcribe_one,
    get_repair_summary,
    run_repair,
)


# ---- helpers --------------------------------------------------------------


def _mock_client(get_responses: list[dict] | None = None, summary: dict | None = None) -> MagicMock:
    """Build a MagicMock LumiverbClient with deterministic .get/.post."""
    client = MagicMock()
    client.base_url = "http://test"
    client.token = "tok"
    if get_responses is not None:
        responses_iter = iter(get_responses)

        def _get(path, params=None):  # noqa: ARG001
            try:
                payload = next(responses_iter)
            except StopIteration:
                payload = {"items": [], "next_cursor": None}
            r = MagicMock()
            r.json.return_value = payload
            return r

        client.get.side_effect = _get
    if summary is not None:
        # repair-summary path returns the summary dict
        original = client.get.side_effect or (lambda *a, **k: MagicMock(json=MagicMock(return_value={})))

        def _get_with_summary(path, params=None):
            if path == "/v1/assets/repair-summary":
                r = MagicMock()
                r.json.return_value = summary
                return r
            return original(path, params=params)

        client.get.side_effect = _get_with_summary
    client.post = MagicMock()
    return client


def _silent_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=200)


# ---- _RepairStats ---------------------------------------------------------


def test_repair_stats_init() -> None:
    s = _RepairStats()
    assert s.processed == 0
    assert s.failed == 0
    assert s.skipped == 0
    # lock is reentrant only in the sense that .acquire/.release work
    s.lock.acquire()
    s.lock.release()


# ---- _page_missing --------------------------------------------------------


def test_page_missing_single_page_each_filter() -> None:
    """Each missing_* flag should be forwarded to the API and the helper
    should return all collected items."""
    client = _mock_client(
        get_responses=[{"items": [{"asset_id": "a"}, {"asset_id": "b"}], "next_cursor": None}]
    )
    out = _page_missing(client, "lib_1", missing_vision=True)
    assert [a["asset_id"] for a in out] == ["a", "b"]
    sent_params = client.get.call_args.kwargs["params"]
    assert sent_params["missing_vision"] == "true"
    assert sent_params["library_id"] == "lib_1"


@pytest.mark.parametrize(
    "kwarg,expected_param",
    [
        ("missing_vision", "missing_vision"),
        ("missing_embeddings", "missing_embeddings"),
        ("missing_faces", "missing_faces"),
        ("missing_video_scenes", "missing_video_scenes"),
        ("missing_ocr", "missing_ocr"),
        ("missing_scene_vision", "missing_scene_vision"),
        ("missing_transcription", "missing_transcription"),
    ],
)
def test_page_missing_forwards_each_flag(kwarg, expected_param) -> None:
    client = _mock_client(get_responses=[{"items": [], "next_cursor": None}])
    _page_missing(client, "lib_x", **{kwarg: True})
    sent_params = client.get.call_args.kwargs["params"]
    assert sent_params[expected_param] == "true"


def test_page_missing_paginates() -> None:
    """Helper should follow next_cursor across multiple pages."""
    client = _mock_client(
        get_responses=[
            {"items": [{"asset_id": "a1"}], "next_cursor": "c1"},
            {"items": [{"asset_id": "a2"}], "next_cursor": "c2"},
            {"items": [{"asset_id": "a3"}], "next_cursor": None},
        ]
    )
    out = _page_missing(client, "lib_p", missing_embeddings=True)
    assert [a["asset_id"] for a in out] == ["a1", "a2", "a3"]
    # Second + third calls should include after=cursor
    assert client.get.call_count == 3
    after_params = [c.kwargs["params"].get("after") for c in client.get.call_args_list]
    assert after_params == [None, "c1", "c2"]


def test_page_missing_breaks_on_empty_items() -> None:
    client = _mock_client(get_responses=[{"items": [], "next_cursor": "should_not_follow"}])
    out = _page_missing(client, "lib_e", missing_ocr=True)
    assert out == []
    assert client.get.call_count == 1  # short-circuit on empty page


def test_page_all_images_filters_to_images() -> None:
    client = _mock_client(
        get_responses=[
            {"items": [{"asset_id": "i1"}, {"asset_id": "i2"}], "next_cursor": None},
        ]
    )
    out = _page_all_images(client, "lib_imgs")
    assert len(out) == 2
    sent_params = client.get.call_args.kwargs["params"]
    assert sent_params["media_type"] == "image"
    assert sent_params["library_id"] == "lib_imgs"


def test_page_all_images_paginates() -> None:
    client = _mock_client(
        get_responses=[
            {"items": [{"asset_id": "i1"}], "next_cursor": "p1"},
            {"items": [{"asset_id": "i2"}], "next_cursor": None},
        ]
    )
    out = _page_all_images(client, "lib_imgs")
    assert [a["asset_id"] for a in out] == ["i1", "i2"]
    assert client.get.call_args_list[1].kwargs["params"]["after"] == "p1"


# ---- get_repair_summary ---------------------------------------------------


def test_get_repair_summary_calls_endpoint() -> None:
    client = _mock_client()
    client.get.side_effect = None
    client.get.return_value = MagicMock(json=MagicMock(return_value={"total_assets": 5}))
    out = get_repair_summary(client, "lib_s")
    assert out == {"total_assets": 5}
    client.get.assert_called_once_with(
        "/v1/assets/repair-summary", params={"library_id": "lib_s"}
    )


# ---- _repair_embed_one ----------------------------------------------------


def test_repair_embed_one_no_proxy_returns_none() -> None:
    proxy_cache = MagicMock()
    proxy_cache.get.return_value = None
    clip = MagicMock(model_id="clip", model_version="1")
    result = _repair_embed_one(
        asset_id="a1",
        rel_path="x.jpg",
        clip_provider=clip,
        proxy_cache=proxy_cache,
    )
    assert result is None
    clip.embed_image.assert_not_called()


def test_repair_embed_one_happy_path() -> None:
    """With a real (1×1) JPEG and a mocked CLIP provider the helper should
    return a result envelope with the embedding vector."""
    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (8, 8), (255, 0, 0)).save(buf, format="JPEG")
    proxy_cache = MagicMock()
    proxy_cache.get.return_value = buf.getvalue()

    clip = MagicMock(model_id="clip", model_version="1")
    clip.embed_image.return_value = [0.1] * 4

    result = _repair_embed_one(
        asset_id="a2",
        rel_path="hello.jpg",
        clip_provider=clip,
        proxy_cache=proxy_cache,
    )
    assert result == {
        "asset_id": "a2",
        "model_id": "clip",
        "model_version": "1",
        "vector": [0.1] * 4,
    }
    clip.embed_image.assert_called_once()


def test_repair_embed_one_no_cache() -> None:
    result = _repair_embed_one(
        asset_id="a3",
        rel_path="hello.jpg",
        clip_provider=MagicMock(),
        proxy_cache=None,
    )
    assert result is None


# ---- _ocr_one -------------------------------------------------------------


def test_ocr_one_returns_text() -> None:
    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (4, 4), (0, 0, 0)).save(buf, format="JPEG")
    proxy_cache = MagicMock()
    proxy_cache.get.return_value = buf.getvalue()
    ocr = MagicMock()
    ocr.extract_text.return_value = "STOP"

    result = _ocr_one(
        asset_id="a", rel_path="x.jpg", ocr_provider=ocr, proxy_cache=proxy_cache
    )
    assert result == {"asset_id": "a", "ocr_text": "STOP"}
    ocr.extract_text.assert_called_once()


def test_ocr_one_no_proxy_returns_none() -> None:
    proxy_cache = MagicMock()
    proxy_cache.get.return_value = None
    result = _ocr_one(
        asset_id="a", rel_path="x.jpg", ocr_provider=MagicMock(), proxy_cache=proxy_cache
    )
    assert result is None


def test_ocr_one_swallows_provider_exception() -> None:
    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (4, 4), (0, 0, 0)).save(buf, format="JPEG")
    proxy_cache = MagicMock()
    proxy_cache.get.return_value = buf.getvalue()
    ocr = MagicMock()
    ocr.extract_text.side_effect = RuntimeError("model exploded")

    result = _ocr_one(
        asset_id="a", rel_path="x.jpg", ocr_provider=ocr, proxy_cache=proxy_cache
    )
    assert result is None


def test_ocr_one_returns_empty_text_on_none() -> None:
    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (4, 4), (0, 0, 0)).save(buf, format="JPEG")
    proxy_cache = MagicMock()
    proxy_cache.get.return_value = buf.getvalue()
    ocr = MagicMock()
    ocr.extract_text.return_value = None

    result = _ocr_one(
        asset_id="a", rel_path="x.jpg", ocr_provider=ocr, proxy_cache=proxy_cache
    )
    assert result == {"asset_id": "a", "ocr_text": ""}


# ---- _transcribe_one ------------------------------------------------------


def test_transcribe_one_no_audio_track(tmp_path: Path) -> None:
    """When ffmpeg reports a stream-less file, the helper returns ('','')."""
    src = tmp_path / "silent.mov"
    src.write_bytes(b"\x00" * 16)

    fake = MagicMock(returncode=1, stderr=b"Output file #0 does not contain any stream")
    with patch("subprocess.run", return_value=fake):
        out = _transcribe_one(src)
    assert out == ("", "")


def test_transcribe_one_returns_srt_on_success(tmp_path: Path) -> None:
    """ffmpeg succeeds, whisper subprocess returns valid JSON."""
    src = tmp_path / "speak.mp4"
    src.write_bytes(b"\x00" * 16)

    ffmpeg_ok = MagicMock(returncode=0, stderr=b"")
    whisper_ok = MagicMock(
        returncode=0,
        stdout='{"srt": "1\\n00:00:00,000 --> 00:00:01,000\\nhi\\n", "language": "en"}',
        stderr="",
    )

    # Make the temp wav report a non-trivial size so the early-empty branch is bypassed
    with patch("subprocess.run", side_effect=[ffmpeg_ok, whisper_ok]), \
         patch("os.path.getsize", return_value=10_000), \
         patch("os.unlink"):
        out = _transcribe_one(src)
    assert out is not None
    srt, lang = out
    assert "00:00:00,000" in srt
    assert lang == "en"


def test_transcribe_one_whisper_subprocess_failure(tmp_path: Path) -> None:
    src = tmp_path / "speak.mp4"
    src.write_bytes(b"\x00" * 16)

    ffmpeg_ok = MagicMock(returncode=0, stderr=b"")
    whisper_fail = MagicMock(returncode=1, stdout="", stderr="boom")

    with patch("subprocess.run", side_effect=[ffmpeg_ok, whisper_fail]), \
         patch("os.path.getsize", return_value=10_000), \
         patch("os.unlink"):
        out = _transcribe_one(src)
    assert out is None  # transient — caller will retry


def test_transcribe_one_invalid_whisper_json(tmp_path: Path) -> None:
    src = tmp_path / "speak.mp4"
    src.write_bytes(b"\x00" * 16)

    ffmpeg_ok = MagicMock(returncode=0, stderr=b"")
    whisper_garbage = MagicMock(returncode=0, stdout="not json", stderr="")

    with patch("subprocess.run", side_effect=[ffmpeg_ok, whisper_garbage]), \
         patch("os.path.getsize", return_value=10_000), \
         patch("os.unlink"):
        out = _transcribe_one(src)
    assert out is None


def test_transcribe_one_short_wav_treated_as_empty(tmp_path: Path) -> None:
    src = tmp_path / "speak.mp4"
    src.write_bytes(b"\x00" * 16)

    ffmpeg_ok = MagicMock(returncode=0, stderr=b"")
    with patch("subprocess.run", return_value=ffmpeg_ok), \
         patch("os.path.getsize", return_value=10), \
         patch("os.unlink"):
        out = _transcribe_one(src)
    assert out == ("", "")


# ---- run_repair (dry_run plan builder) -----------------------------------


def _summary_with(**counts) -> dict:
    base = {
        "total_assets": 100,
        "missing_proxy": 0,
        "missing_exif": 0,
        "missing_embeddings": 0,
        "missing_vision": 0,
        "missing_faces": 0,
        "missing_ocr": 0,
        "missing_transcription": 0,
        "missing_video_scenes": 0,
        "missing_scene_vision": 0,
        "stale_search_sync": 0,
    }
    base.update(counts)
    return base


def test_run_repair_dry_run_nothing_to_do() -> None:
    """An empty repair summary should print 'Nothing to repair.' and return."""
    summary = _summary_with()
    client = MagicMock()
    client.get.return_value = MagicMock(json=MagicMock(return_value=summary))

    file = io.StringIO()
    console = Console(file=file, force_terminal=False, width=200)
    run_repair(
        client=client,
        library={"library_id": "lib_x", "name": "Lib", "root_path": None},
        job_type="all",
        dry_run=True,
        console=console,
    )
    out = file.getvalue()
    assert "Nothing to repair" in out
    # No POSTs should have been made when there's nothing to do
    client.post.assert_not_called()


def test_run_repair_dry_run_builds_plan_for_each_type() -> None:
    """Plan builder should pick up every missing_* count and report dry-run."""
    summary = _summary_with(
        missing_embeddings=3,
        missing_vision=2,
        missing_faces=1,
        missing_ocr=4,
        missing_transcription=1,
        missing_video_scenes=2,
        missing_scene_vision=1,
        stale_search_sync=5,
    )
    client = MagicMock()
    client.get.return_value = MagicMock(json=MagicMock(return_value=summary))

    file = io.StringIO()
    console = Console(file=file, force_terminal=False, width=200)
    run_repair(
        client=client,
        library={"library_id": "lib_y", "name": "Y", "root_path": None},
        job_type="all",
        dry_run=True,
        console=console,
    )
    out = file.getvalue()
    # The summary table should include each label, with non-zero counts
    for label in (
        "Embeddings",
        "Vision AI",
        "Faces",
        "OCR",
        "Transcription",
        "Video scenes",
        "Scene vision",
        "Search sync",
    ):
        assert label in out
    assert "--dry-run" in out
    client.post.assert_not_called()


def test_run_repair_force_search_sync_full_reindex() -> None:
    """With --force, search-sync should plan a full re-index even when
    stale_search_sync is 0. Dry-run hides the per-type description, so we
    assert the dry-run gate fired (plan was non-empty) instead."""
    summary = _summary_with(stale_search_sync=0, total_assets=42)
    client = MagicMock()
    client.get.return_value = MagicMock(json=MagicMock(return_value=summary))

    file = io.StringIO()
    console = Console(file=file, force_terminal=False, width=200)
    run_repair(
        client=client,
        library={"library_id": "lib_z", "name": "Z", "root_path": None},
        job_type="search-sync",
        dry_run=True,
        force=True,
        console=console,
    )
    out = file.getvalue()
    # Dry-run gate fired (so plan was non-empty) and the "nothing to repair"
    # path was not taken.
    assert "--dry-run" in out
    assert "Nothing to repair" not in out

    # And without --force, an empty stale count should produce a no-op.
    file2 = io.StringIO()
    console2 = Console(file=file2, force_terminal=False, width=200)
    run_repair(
        client=client,
        library={"library_id": "lib_z", "name": "Z", "root_path": None},
        job_type="search-sync",
        dry_run=True,
        force=False,
        console=console2,
    )
    assert "Nothing to repair" in file2.getvalue()


def test_run_repair_skip_types_filter() -> None:
    """skip_types should drop entries from the plan even when job_type='all'."""
    summary = _summary_with(missing_embeddings=2, missing_vision=2)
    client = MagicMock()
    client.get.return_value = MagicMock(json=MagicMock(return_value=summary))

    file = io.StringIO()
    console = Console(file=file, force_terminal=False, width=200)
    run_repair(
        client=client,
        library={"library_id": "lib_s", "name": "S", "root_path": None},
        job_type="all",
        dry_run=True,
        skip_types={"embed", "vision"},
        console=console,
    )
    out = file.getvalue()
    # With both items skipped from the plan, the dry-run wraps with
    # 'Nothing to repair' (the table is still printed, but no plan).
    assert "Nothing to repair" in out


@pytest.mark.parametrize("job_type,key,desc", [
    ("embed", "missing_embeddings", "missing CLIP embeddings"),
    ("vision", "missing_vision", "missing AI descriptions"),
    ("faces", "missing_faces", "missing face detection"),
    ("ocr", "missing_ocr", "missing OCR text"),
    ("transcribe", "missing_transcription", "missing transcription"),
    ("video-scenes", "missing_video_scenes", "missing video scene detection"),
    ("scene-vision", "missing_scene_vision", "missing scene vision AI"),
])
def test_run_repair_dry_run_per_type(job_type, key, desc) -> None:
    summary = _summary_with(**{key: 7})
    client = MagicMock()
    client.get.return_value = MagicMock(json=MagicMock(return_value=summary))

    file = io.StringIO()
    console = Console(file=file, force_terminal=False, width=200)
    run_repair(
        client=client,
        library={"library_id": "lib_p", "name": "P", "root_path": None},
        job_type=job_type,
        dry_run=True,
        console=console,
    )
    out = file.getvalue()
    assert "--dry-run" in out  # confirms plan was non-empty and dry-run hit
    client.post.assert_not_called()


def test_run_repair_redetect_faces_dry_run_pages_all_images(tmp_path: Path) -> None:
    """redetect-faces dry-run should call _page_all_images and stop at the dry-run gate."""
    summary = _summary_with()  # nothing missing — but redetect-faces is independent
    client = MagicMock()

    def _get(path, params=None):  # noqa: ARG001
        r = MagicMock()
        if path == "/v1/assets/repair-summary":
            r.json.return_value = summary
        else:
            r.json.return_value = {
                "items": [{"asset_id": "img1"}, {"asset_id": "img2"}],
                "next_cursor": None,
            }
        return r

    client.get.side_effect = _get
    file = io.StringIO()
    console = Console(file=file, force_terminal=False, width=200)
    run_repair(
        client=client,
        library={"library_id": "lib_rd", "name": "RD", "root_path": str(tmp_path)},
        job_type="redetect-faces",
        dry_run=True,
        console=console,
    )
    out = file.getvalue()
    # Dry-run hides per-type plan descriptions; assert the page-all-images
    # API was hit and the dry-run gate fired (plan was non-empty).
    paged = any(
        c.kwargs.get("params", {}).get("media_type") == "image"
        for c in client.get.call_args_list
    )
    assert paged
    assert "--dry-run" in out
    assert "Nothing to repair" not in out
    client.post.assert_not_called()
