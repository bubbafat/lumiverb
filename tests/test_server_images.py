"""The API server reads only JPEG, PNG and WebP, up to a pixel limit.

Wherever it decodes a picture it was sent or has stored (src/server/images.py:
previews at ingest and upload, the picture a search is by, a proxy cut into
face crops or embedded, a video face's crop), no other format's reader looks
at the bytes, and an image over the limit is refused from its header, before
any pixel is decoded. The search routes' answers are in
tests/test_similarity_slow.py.
"""

from __future__ import annotations

import io
import logging
import struct
import warnings
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageFile

pytestmark = pytest.mark.fast

READ = ("JPEG", "PNG", "WEBP")
NOT_READ = ("EPS", "TIFF", "GIF", "BMP", "PSD", "garbage")
REFUSED_FORMAT = "isn't a JPEG, PNG or WebP image"


def _psd(w: int = 8, h: int = 8) -> bytes:
    """A Photoshop file Pillow reads (it doesn't write them): 8-bit RGB, raw."""
    header = b"8BPS" + struct.pack(">H", 1) + b"\x00" * 6 + struct.pack(">HIIHH", 3, h, w, 8, 3)
    return header + struct.pack(">III", 0, 0, 0) + struct.pack(">H", 0) + b"\x80" * (3 * w * h)


def sample(fmt: str, size: tuple[int, int] = (64, 48)) -> bytes:
    """A small picture in fmt, or bytes that are none."""
    if fmt == "PSD":
        return _psd(*size)
    if fmt == "garbage":
        return b"\x00\x01not a picture\xfe" * 64
    buf = io.BytesIO()
    Image.new("RGB", size, (90, 140, 200)).save(buf, fmt)
    return buf.getvalue()


def sparse_png(width: int, height: int) -> bytes:
    """A valid 1-bit PNG of width x height, all black: some KB however many
    pixels, as an oversized image can be."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"\x00" * (1 + (width + 7) // 8)  # filter byte + packed bits
    per = max(1, 4_000_000 // len(row))
    z = zlib.compressobj(9)
    parts, left = [], height
    while left:
        n = min(left, per)
        parts.append(z.compress(row * n))
        left -= n
    parts.append(z.flush())
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0))
            + chunk(b"IDAT", b"".join(parts)) + chunk(b"IEND", b""))


# ~170 megapixels: over the limit, under Pillow's own refusal (2 x 89.5 MP).
OVERSIZED = (13_040, 13_040)


@pytest.fixture
def other_readers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The formats outside JPEG, PNG and WebP whose readers were asked to open something."""
    Image.init()
    asked: list[str] = []
    for fmt, (factory, accept) in list(Image.OPEN.items()):
        if fmt in READ:
            continue

        def spy(fp, filename=None, *, _fmt=fmt, _factory=factory):
            asked.append(_fmt)
            return _factory(fp, filename)

        monkeypatch.setitem(Image.OPEN, fmt, (spy, accept))
    return asked


@pytest.fixture
def no_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any image decoded fails the test (a PNG decodes in ImageFile.load)."""
    def load(self):
        raise AssertionError(f"decoded a {self.size[0]} x {self.size[1]} image")

    monkeypatch.setattr(ImageFile.ImageFile, "load", load)


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_the_samples_are_what_they_say():
    for fmt in NOT_READ[:-1]:
        with Image.open(io.BytesIO(sample(fmt))) as img:
            assert img.format == fmt
            if fmt != "EPS":  # EPS decodes through Ghostscript, where it's installed
                img.load()
    with Image.open(io.BytesIO(sparse_png(*OVERSIZED))) as img:
        assert img.size == OVERSIZED and img.mode == "1"
    assert len(sparse_png(*OVERSIZED)) < 100_000


# ---------------------------------------------------------------------------
# src/server/images.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", READ)
def test_the_server_reads_jpeg_png_and_webp(fmt, other_readers):
    from src.server import images

    with images.opened(sample(fmt)) as img:
        assert img.format == fmt and img.size == (64, 48)
    with images.decoded(sample(fmt)) as img:
        assert img.size == (64, 48)
    img = images.rgb(sample(fmt))
    assert img.mode == "RGB" and img.size == (64, 48)
    assert other_readers == []


@pytest.mark.parametrize("fmt", NOT_READ)
def test_the_server_reads_no_other_format(fmt, other_readers):
    from src.server import images

    for read in (images.opened, images.decoded, images.rgb):
        with pytest.raises(images.NotAnImageError, match=REFUSED_FORMAT):
            read(sample(fmt))
    assert other_readers == [], "no reader but JPEG's, PNG's and WebP's looks at the bytes"


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")  # Pillow's, under its own refusal
def test_an_image_over_the_pixel_limit_is_refused_before_it_decodes(no_decoding):
    from src.server import images

    big = sparse_png(*OVERSIZED)
    for read in (images.opened, images.decoded, images.rgb):
        with pytest.raises(images.NotAnImageError, match="is 13040 x 13040 pixels, more than the 120 megapixels"):
            read(big)


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_a_100_megapixel_photo_is_read():
    from src.server import images

    camera = (11_656, 8_742)  # a 100 MP medium-format camera's photo
    with images.opened(sparse_png(*camera)) as img:
        assert img.size == camera
    assert camera[0] * camera[1] <= images.MAX_PIXELS < OVERSIZED[0] * OVERSIZED[1]


def test_pillows_own_size_limit_is_a_refusal_not_a_crash(no_decoding):
    from src.server import images

    with pytest.raises(images.NotAnImageError, match="has more pixels than the server reads"):
        images.opened(sparse_png(14_000, 14_000))  # 196 MP: Pillow refuses it
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)  # a process where warnings are errors
        with pytest.raises(images.NotAnImageError, match="has more pixels than the server reads"):
            images.opened(sparse_png(*OVERSIZED))


def test_a_picture_that_doesnt_decode_is_refused():
    from src.server import images

    buf = io.BytesIO()
    Image.effect_noise((400, 300), 64).save(buf, "PNG")
    truncated = buf.getvalue()[: len(buf.getvalue()) // 2]
    with images.opened(truncated):
        pass  # the header is whole
    for read in (images.decoded, images.rgb):
        with pytest.raises(images.NotAnImageError, match="doesn't decode"):
            read(truncated)


# ---------------------------------------------------------------------------
# Each place the server decodes a picture
# ---------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_a_preview_over_the_pixel_limit_is_refused_before_it_decodes(no_decoding):
    from src.server.preview_image import NotAnImageError, reencoded, thumbnail

    big = sparse_png(*OVERSIZED)
    with pytest.raises(NotAnImageError):
        reencoded(big, fmt="WEBP", quality=80)
    with pytest.raises(NotAnImageError):
        thumbnail(big)


@pytest.mark.parametrize("fmt", ["EPS", "TIFF", "GIF", "PSD"])
def test_previews_are_read_only_as_jpeg_png_and_webp(fmt, other_readers):
    from src.server.preview_image import NotAnImageError, reencoded, thumbnail

    with pytest.raises(NotAnImageError):
        reencoded(sample(fmt), fmt="WEBP", quality=80)
    with pytest.raises(NotAnImageError):
        thumbnail(sample(fmt))
    assert other_readers == []


def test_face_detection_reads_only_jpeg_png_and_webp(monkeypatch, other_readers):
    from src.server.faces import server_face_provider

    handed: list[bytes] = []
    child = SimpleNamespace(detect=lambda image, settings: handed.append(image) or {"faces": []})
    monkeypatch.setattr(server_face_provider, "_the_detector", lambda: child)

    for fmt in NOT_READ:
        with pytest.raises(ValueError, match=REFUSED_FORMAT):
            server_face_provider.detect_faces_in_image(sample(fmt))
    assert handed == [], "refused here, never handed to the face process"
    assert other_readers == []

    for fmt in READ:
        assert server_face_provider.detect_faces_in_image(sample(fmt)) == []
    assert handed == [sample(fmt) for fmt in READ]


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_face_detection_refuses_an_image_over_the_pixel_limit(monkeypatch, no_decoding):
    from src.server.faces import server_face_provider

    handed: list[bytes] = []
    monkeypatch.setattr(server_face_provider, "_the_detector",
                        lambda: SimpleNamespace(detect=lambda image, settings: handed.append(image) or {"faces": []}))
    with pytest.raises(ValueError, match="more than the 120 megapixels"):
        server_face_provider.detect_faces_in_image(sparse_png(*OVERSIZED))
    assert handed == []


def test_clip_embeds_only_jpeg_png_and_webp(monkeypatch, tmp_path: Path, other_readers):
    from src.server.embeddings.clip_provider import CLIPEmbeddingProvider

    seen: list[tuple[str, tuple[int, int]]] = []
    monkeypatch.setattr(CLIPEmbeddingProvider, "embed_image",
                        lambda self, img: seen.append((img.mode, img.size)) or [0.5])
    provider = CLIPEmbeddingProvider()
    for fmt in (*NOT_READ, *READ):
        (tmp_path / fmt).write_bytes(sample(fmt))

    for fmt in NOT_READ:
        with pytest.raises(ValueError, match=REFUSED_FORMAT):
            provider.embed(tmp_path / fmt)
    assert seen == [] and other_readers == []
    for fmt in READ:
        assert provider.embed(tmp_path / fmt) == [0.5]
    assert seen == [("RGB", (64, 48))] * 3


FACE = "face_01HZ0000000000000000000003"


def _face_crops(monkeypatch, tmp_path: Path, proxy: bytes) -> tuple[list[str], SimpleNamespace]:
    """Cut a face's crop from a photo whose stored proxy is `proxy`: (the keys
    written, the session)."""
    from unittest.mock import MagicMock

    from src.server.api.routers.assets import _generate_face_crops
    from src.server.storage import store as store_module
    from src.server.storage.files import FileStore
    from src.server.storage.local import LocalStorage

    store = FileStore(LocalStorage(str(tmp_path)))
    store.write("ten_1/lib_1/proxies/00/ast_1_photo.webp", proxy)
    monkeypatch.setattr(store_module, "get_store", lambda: store)
    session = MagicMock()
    _generate_face_crops("ten_1", SimpleNamespace(proxy_key="ten_1/lib_1/proxies/00/ast_1_photo.webp",
                                                  library_id="lib_1"), [FACE],
                         [{"bounding_box": {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}}], session)
    return list(store.list("ten_1/lib_1/face_crops/")), session


@pytest.mark.parametrize("fmt", READ)
def test_a_photos_face_crop_is_cut_from_a_jpeg_png_or_webp_proxy(fmt, monkeypatch, tmp_path):
    written, session = _face_crops(monkeypatch, tmp_path, sample(fmt))
    assert written == ["ten_1/lib_1/face_crops/79/face_01HZ0000000000000000000003.webp"] and session.execute.called


@pytest.mark.parametrize("fmt", NOT_READ)
def test_no_face_crop_is_cut_from_a_proxy_in_another_format(fmt, monkeypatch, tmp_path, other_readers, caplog):
    caplog.set_level(logging.WARNING)
    written, session = _face_crops(monkeypatch, tmp_path, sample(fmt))
    assert written == [] and not session.execute.called
    assert "Cannot open proxy for face crops" in caplog.text
    assert other_readers == []


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_no_face_crop_is_cut_from_a_proxy_over_the_pixel_limit(monkeypatch, tmp_path, no_decoding, caplog):
    caplog.set_level(logging.WARNING)
    written, session = _face_crops(monkeypatch, tmp_path, sparse_png(*OVERSIZED))
    assert written == [] and not session.execute.called
    assert "more than the 120 megapixels" in caplog.text


def test_a_video_faces_crop_is_read_only_as_webp(other_readers):
    from src.server.api.routers.video import _webp

    assert _webp(sample("WEBP")) is True
    for fmt in ("JPEG", "PNG", *NOT_READ):
        assert _webp(sample(fmt)) is False, fmt
    assert other_readers == [], "only WebP's reader looks at a crop"
