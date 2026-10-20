"""docs/cursor-api.md is a client's whole contract (ADR-020 decision 9): what
the server needs from a client is specified there, not only in its code. So
what "Writing a client: a library's reader" states as numbers and lists is
held here to the code that decides them: a change to one fails until the
other says the same.

Read as text: the section's own words, as a person building a client reads
them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

DOC = Path(__file__).resolve().parents[1] / "docs" / "cursor-api.md"


def _section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("## Writing a client: a library's reader\n")
    return text[start:text.index("\n## ", start + 1)]


def _extensions(label: str) -> set[str]:
    [line] = [l for l in _section().splitlines() if l.strip().startswith(f"- {label}:")]
    return set(re.findall(r"`(\.[a-z0-9]+)`", line))


def test_the_media_it_lists_are_the_ones_the_server_takes():
    from src.shared.file_extensions import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

    assert _extensions("Images") == IMAGE_EXTENSIONS
    assert _extensions("Videos") == VIDEO_EXTENSIONS


def test_the_pictures_it_describes_are_the_ones_the_server_reads_and_keeps():
    from src.server import images, preview_image
    from src.server.api.routers import ingest
    from src.shared.producers import PRODUCERS

    said = _section()
    names = {"JPEG": "JPEG", "PNG": "PNG", "WEBP": "WebP"}
    assert ", ".join(names[f] for f in images.FORMATS[:-1]) + f" or {names[images.FORMATS[-1]]}" in said
    assert f"at most {images.MAX_PIXELS // 1_000_000} megapixels" in said
    assert f"At most {ingest.MAX_PROXY_UPLOAD_BYTES // 2**20} MB at ingest" in said
    assert f"at most {ingest.PROXY_MAX_LONG_EDGE:,} px on its long edge" in said
    assert f"send it at {PRODUCERS['lumiverb.preview'].defaults['long_edge']:,} px" in said
    assert f"WebP at {preview_image.THUMBNAIL_LONG_EDGE} px" in said
    assert PRODUCERS["lumiverb.preview"].defaults["thumbnail_edge"] == preview_image.THUMBNAIL_LONG_EDGE


def test_the_videos_it_describes_are_the_ones_lumiverb_makes():
    from src.processing.video.analysis_proxy import KEYFRAME_SEC
    from src.producers.names import ANALYSIS_PROXY
    from src.server.api.routers import artifacts
    from src.shared.producers import PRODUCERS

    said = _section()
    preview, proxy = PRODUCERS["lumiverb.video.preview"].defaults, PRODUCERS[ANALYSIS_PROXY].defaults
    assert f"by default its first {preview['seconds']} seconds, at most {preview['max_height']} px high" in said
    assert (f"by default at most {proxy['max_edge']} px on its long edge and {proxy['fps_max']} frames a second"
            in said)
    assert f"Its keyframes are at 0 and every {KEYFRAME_SEC} seconds after, and nowhere else" in said
    assert f"Up to {artifacts.MAX_UPLOAD_BYTES // 2**20} MB an upload" in said
    assert f"an analysis proxy up to {artifacts.MAX_UPLOAD_BYTES_BY_TYPE[ANALYSIS_PROXY] // 2**30} GiB" in said


def test_the_scan_it_describes_waits_for_files_as_lumiverb_s_does():
    from src.processing.scan import FUTURE_MTIME_SEC, SETTLE_SEC

    said = _section()
    assert f"modified in the last {SETTLE_SEC} seconds" in said
    assert FUTURE_MTIME_SEC // 60 >= 2 and "A modified time minutes ahead is a camera's clock" in said


def test_what_it_sends_by_name_is_what_lumiverb_s_scan_sends():
    # ADR-024 phase 1 part 2: the preview, each lineage and the video preview by their names, at protocol 2.
    from src.producers.names import CAPTURE_TIME, PREVIEW, VIDEO_FORMAT, VIDEO_PREVIEW
    from src.shared.readers import PROTOCOL

    said = _section()
    assert f"Speak `protocol` {PROTOCOL}" in said
    assert f"   - `{PREVIEW}`: its preview" in said
    for name in (PREVIEW, CAPTURE_TIME, VIDEO_FORMAT):
        assert f"`{name}`" in said, name
    assert f"`POST /v1/assets/{{id}}/artifacts/{VIDEO_PREVIEW}`" in said


def test_a_change_to_the_code_would_fail_it(monkeypatch):
    from src.shared import file_extensions

    monkeypatch.setattr(file_extensions, "VIDEO_EXTENSIONS", file_extensions.VIDEO_EXTENSIONS | {".avi"})
    with pytest.raises(AssertionError):
        test_the_media_it_lists_are_the_ones_the_server_takes()
