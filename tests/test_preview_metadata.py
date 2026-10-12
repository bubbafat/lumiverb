"""Previews carry the picture and nothing else of the original's metadata.

Proxies, thumbnails and scene frames are what visitors to a public page are
shown, so none of the original's EXIF (where, when, which camera), XMP or
comments may survive into one, whichever way it was made: by libvips
(src/processing/proxy/proxy_gen.py, src/processing/ingest.py) or sent by a
client and re-encoded by the server (src/server/preview_image.py, the ingest
and artifact routes, tests/test_artifacts.py). The rotation is applied to the
pixels, and the colour profile is kept, so the picture looks as it did.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageCms, PngImagePlugin

pytestmark = pytest.mark.fast

# What a photo below carries, and must not reach a preview: its EXIF (with
# GPS and the camera), its XMP, and their containers.
MAKE = b"TestCamMakeQ7"
XMP_MARK = b"PlaceOnlyInXMPQ7"
METADATA = [MAKE, XMP_MARK, b"Exif\x00\x00", b"http://ns.adobe.com/xap/1.0/"]

_XMP = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" dc:source="' + XMP_MARK + b'"/>'
        b"</rdf:RDF></x:xmpmeta>")


def photo(fmt: str = "JPEG") -> bytes:
    """300×200 pixels to be shown turned (EXIF orientation 6, so 200×300),
    with GPS, a camera, XMP and an sRGB profile."""
    img = Image.new("RGB", (300, 200), (200, 60, 40))
    img.paste((20, 120, 220), (0, 0, 60, 200))
    exif = Image.Exif()
    exif[0x010F] = MAKE.decode()
    exif[0x0110] = "ModelQ7"
    exif[0x0112] = 6
    exif[0x8825] = {1: "N", 2: (48.0, 51.0, 30.24), 3: "E", 4: (2.0, 17.0, 40.2)}
    buf = io.BytesIO()
    if fmt == "PNG":  # PNG keeps XMP in an iTXt chunk
        text = PngImagePlugin.PngInfo()
        text.add_itxt("XML:com.adobe.xmp", _XMP.decode())
        img.save(buf, fmt, exif=exif.tobytes(), icc_profile=_SRGB, pnginfo=text)
    else:
        img.save(buf, fmt, exif=exif.tobytes(), icc_profile=_SRGB, xmp=_XMP)
    data = buf.getvalue()
    assert all(m in data for m in (MAKE, XMP_MARK)), "the photo carries what the tests look for"
    return data


_SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def upright_with_profile(data: bytes, *, profile: bool = True) -> bool:
    """Turned as the photo is shown, and (where asked) its colour profile kept."""
    img = Image.open(io.BytesIO(data))
    return img.size == (200, 300) and (not profile or bool(img.info.get("icc_profile")))


def _no_metadata(data: bytes) -> None:
    assert [m for m in METADATA if m in data] == []
    exif = Image.open(io.BytesIO(data)).getexif()
    assert 0x010F not in exif and not exif.get_ifd(0x8825), "no camera, no GPS"


@pytest.fixture
def photo_file(tmp_path: Path) -> Path:
    path = tmp_path / "IMG_0001.jpg"
    path.write_bytes(photo("JPEG"))
    return path


def test_a_libvips_proxy_keeps_the_picture_and_its_profile_only(photo_file):
    from src.processing.proxy import proxy_gen

    proxy, _, _ = proxy_gen.generate_proxy_bytes(photo_file)
    _no_metadata(proxy)
    assert upright_with_profile(proxy, profile=proxy_gen.KEEP == "keep=icc")


def test_the_scans_webp_keeps_the_picture_and_its_profile_only(photo_file):
    from src.processing.ingest import _jpeg_to_webp
    from src.processing.proxy import proxy_gen

    # The scan's WebP is made from libvips's JPEG; given the original, it still drops it all.
    webp = _jpeg_to_webp(photo("JPEG"))
    _no_metadata(webp)
    assert Image.open(io.BytesIO(webp)).format == "WEBP"
    webp = _jpeg_to_webp(proxy_gen.generate_proxy_bytes(photo_file)[0])
    _no_metadata(webp)
    assert upright_with_profile(webp, profile=proxy_gen.KEEP == "keep=icc")


@pytest.mark.parametrize("fmt", ["JPEG", "WEBP", "PNG"])
def test_the_server_reencodes_a_clients_preview_to_the_picture_only(fmt):
    from src.server.preview_image import reencoded

    data, w, h = reencoded(photo(fmt), fmt="WEBP", quality=80)
    _no_metadata(data)
    assert upright_with_profile(data) and (w, h) == (200, 300)


def test_the_servers_ingest_proxy_is_the_picture_only():
    from src.server.api.routers.ingest import _normalize_proxy

    data, w, h = _normalize_proxy(photo("WEBP"))
    _no_metadata(data)
    assert upright_with_profile(data) and (w, h) == (200, 300)


def test_the_server_reads_only_jpeg_png_and_webp():
    from src.server.preview_image import NotAnImageError, reencoded

    tiff = io.BytesIO()
    Image.new("RGB", (8, 8)).save(tiff, "TIFF")
    for data in (tiff.getvalue(), b"\xff\xd8\xff not really", b""):
        with pytest.raises(NotAnImageError):
            reencoded(data, fmt="WEBP", quality=80)
