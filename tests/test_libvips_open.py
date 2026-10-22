"""libvips opens a library's file by its exact name, with its trusted loaders
only (src/processing/libvips.py).

libvips reads a name ending in [...] as load options, and picks a loader by
a file's content, not its name. A proxy is made from the file the name
names, read as it is, and a file only an untrusted loader reads (ImageMagick,
PDF, SVG, PPM: those libvips itself marks untrusted) makes none. Every
format a library holds still makes its proxy: JPEG, PNG, WebP, TIFF and
HEIF (an iPhone's HEIC) through libvips, BMP through Pillow, RAW through
rawpy (a DNG rawpy can't read through libvips, as a TIFF). A .tif is read as
a TIFF only, whatever Pillow would make of its content, and nothing over
proxy_gen.MAX_PIXELS is decoded: its size is read from its header first.
A libvips older than 8.13, which can't block its untrusted loaders, is
refused where it comes in (a scan), saying so; nothing else stops.
"""

from __future__ import annotations

import ast
import importlib
import io
import struct
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image, UnidentifiedImageError

pytestmark = pytest.mark.fast

SIZE = (1200, 900)  # under the proxy's 2048, so a proxy is the original's size


def picture(size: tuple[int, int] = SIZE) -> Image.Image:
    img = Image.new("RGB", size, (200, 60, 40))
    img.paste((20, 120, 220), (0, 0, size[0] // 4, size[1]))
    return img


def encoded(fmt: str, size: tuple[int, int] = SIZE) -> bytes:
    buf = io.BytesIO()
    picture(size).save(buf, fmt)
    return buf.getvalue()


def proxy_size(proxy: bytes) -> tuple[int, int]:
    img = Image.open(io.BytesIO(proxy))
    assert img.format == "JPEG"
    return img.size


# ---------------------------------------------------------------------------
# A DNG made here: a 16-bit RGGB mosaic in a TIFF directory, as rawpy (LibRaw)
# reads one, and, given its size, a JPEG preview the camera put first.

BYTE, SHORT, LONG = 1, 3, 4


def _ifd(at: int, entries: list[tuple[int, int, list[int]]]) -> bytes:
    """A TIFF directory at offset `at`, the values that don't fit in an entry right after it."""
    extra_at = at + 2 + 12 * len(entries) + 4
    body, extra = struct.pack("<H", len(entries)), b""
    for tag, typ, values in sorted(entries):
        raw = struct.pack("<" + {BYTE: "B", SHORT: "H", LONG: "I"}[typ] * len(values), *values)
        if len(raw) <= 4:
            field = raw.ljust(4, b"\0")
        else:
            field = struct.pack("<I", extra_at + len(extra))
            extra += raw + b"\0" * (len(raw) % 2)
        body += struct.pack("<HHI", tag, typ, len(values)) + field
    return body + struct.pack("<I", 0) + extra


def dng(size: tuple[int, int], preview: tuple[int, int] | None = None) -> bytes:
    w, h = size
    out = bytearray(b"II*\0\0\0\0\0")
    if preview:
        jpeg_at, jpeg = len(out), encoded("JPEG", preview)
        out += jpeg + b"\0" * (len(jpeg) % 2)
    cfa_at, cfa = len(out), struct.pack("<H", 2000) * (w * h)
    out += cfa
    raw_at = len(out)
    out += _ifd(raw_at, [
        (254, LONG, [0]), (256, LONG, [w]), (257, LONG, [h]), (258, SHORT, [16]), (259, SHORT, [1]),
        (262, SHORT, [32803]), (273, LONG, [cfa_at]), (277, SHORT, [1]), (278, LONG, [h]),
        (279, LONG, [len(cfa)]), (33421, SHORT, [2, 2]), (33422, BYTE, [0, 1, 1, 2]),
    ])
    out += b"\0" * (len(out) % 2)
    if preview:  # the preview, JPEG in YCbCr
        first = [(254, LONG, [1]), (256, LONG, [preview[0]]), (257, LONG, [preview[1]]),
                 (258, SHORT, [8, 8, 8]), (259, SHORT, [7]), (262, SHORT, [6]), (273, LONG, [jpeg_at]),
                 (277, SHORT, [3]), (278, LONG, [preview[1]]), (279, LONG, [len(jpeg)])]
    else:  # a 1×1 thumbnail
        first = [(254, LONG, [1]), (256, LONG, [1]), (257, LONG, [1]), (258, SHORT, [8]), (259, SHORT, [1]),
                 (262, SHORT, [1]), (273, LONG, [cfa_at]), (277, SHORT, [1]), (278, LONG, [1]), (279, LONG, [1])]
    first_at = len(out)
    out += _ifd(first_at, first + [(330, LONG, [raw_at]), (50706, BYTE, [1, 4, 0, 0])])  # SubIFDs, DNGVersion
    out[4:8] = struct.pack("<I", first_at)
    return bytes(out)


# ---------------------------------------------------------------------------
# By its exact name


@pytest.mark.parametrize(
    "name, fmt",
    [
        ("photo[shrink=8].jpg", "JPEG"),
        ("album[shrink=8]/photo.jpg", "JPEG"),
        ("scan[shrink=8].tif", "TIFF"),
        ("photo.jpg[shrink=8]", "JPEG"),  # what libvips would read as photo.jpg, shrunk
    ],
)
def test_a_photo_is_opened_by_its_exact_name_whatever_brackets_it_has(tmp_path, name, fmt):
    from src.processing.proxy import proxy_gen

    source = tmp_path / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(encoded(fmt))
    # The file a name read as options would open instead: smaller, so it can't pass for the original.
    (tmp_path / "photo.jpg").write_bytes(encoded("JPEG", (300, 200)))

    proxy, w, h = proxy_gen.generate_proxy_bytes(source)

    assert (w, h) == SIZE
    assert proxy_size(proxy) == SIZE, "the whole picture, not one shrunk on load"


# ---------------------------------------------------------------------------
# Every format a library holds, with the untrusted loaders blocked

RASTER = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".tif": "TIFF", ".tiff": "TIFF",
          ".bmp": "BMP"}
# Pillow writes no HEIF: an iPhone-style HEIC made by macOS's own encoder (sips), a drawn picture, 96 x 64: its
# left half red, its right half blue, a green band along its top (its date, zone and GPS written by exiftool,
# for tests/test_capture.py).
HEIC = Path(__file__).parent / "fixtures" / "drawn.heic"
HEIF = {".heic", ".heif"}


def test_every_format_a_library_holds_is_tested_here():
    from src.shared.file_extensions import IMAGE_EXTENSIONS, RAW_EXTENSIONS

    assert set(RASTER) | HEIF == IMAGE_EXTENSIONS - RAW_EXTENSIONS, "a new format needs its proxy tested below"


@pytest.mark.parametrize("name", ["IMG_0001.HEIC", "IMG_0002.heic", "photo.heif"])
def test_an_iphones_heic_makes_its_proxy_the_right_way_round(tmp_path, name):
    from src.processing.proxy import proxy_gen

    source = tmp_path / name
    source.write_bytes(HEIC.read_bytes())

    proxy, w, h = proxy_gen.generate_proxy_bytes(source)

    assert (w, h) == (96, 64) and proxy_size(proxy) == (96, 64)
    img = Image.open(io.BytesIO(proxy)).convert("RGB")

    def near(xy, want):
        return all(abs(a - b) < 40 for a, b in zip(img.getpixel(xy), want, strict=True))

    assert near((12, 40), (255, 0, 0)) and near((84, 40), (0, 0, 255)), "red left, blue right: not mirrored"
    assert near((48, 2), (0, 200, 0)), "the green band on top: not upside down"


def test_libvips_reads_heif_with_a_trusted_loader():
    # Were it ever marked untrusted, blocking those (src/processing/libvips.py) would stop every HEIC photo.
    from src.processing.proxy import proxy_gen

    pyvips = proxy_gen.pyvips
    assert pyvips.type_find("VipsOperation", "heifload_source"), "this libvips reads no HEIF: libheif is missing"
    assert not pyvips.Introspect.get("heifload_source").flags & _UNTRUSTED


@pytest.mark.parametrize("ext", sorted(RASTER))
def test_each_raster_format_a_library_holds_makes_its_proxy(tmp_path, ext):
    from src.processing.proxy import proxy_gen

    source = tmp_path / f"photo{ext}"
    source.write_bytes(encoded(RASTER[ext]))

    proxy, w, h = proxy_gen.generate_proxy_bytes(source)

    assert (w, h) == SIZE and proxy_size(proxy) == SIZE


def test_a_raw_rawpy_demosaics_makes_its_proxy(tmp_path):
    from src.processing.proxy import proxy_gen

    source = tmp_path / "IMG_0001.dng"
    source.write_bytes(dng((640, 480)))

    proxy, w, h = proxy_gen.generate_proxy_bytes(source)

    assert (w, h) == (640, 480) and proxy_size(proxy) == (640, 480)


def test_a_raws_own_preview_makes_its_proxy(tmp_path):
    from src.processing.proxy import proxy_gen

    source = tmp_path / "IMG_0002.dng"
    source.write_bytes(dng((640, 480), preview=(2100, 1400)))

    proxy, w, h = proxy_gen.generate_proxy_bytes(source)

    assert (w, h) == (640, 480), "the sensor's size"
    assert proxy_size(proxy) == (2048, 1365), "the preview, made the proxy's size"


def test_a_dng_rawpy_cant_read_is_read_by_libvips_as_the_tiff_it_is(tmp_path):
    from src.processing.proxy import proxy_gen

    source = tmp_path / "IMG_0003.dng"
    source.write_bytes(encoded("TIFF"))

    proxy, w, h = proxy_gen.generate_proxy_bytes(source)

    assert (w, h) == SIZE and proxy_size(proxy) == SIZE


# ---------------------------------------------------------------------------
# A TIFF as a TIFF, and nothing decoded over MAX_PIXELS


@pytest.mark.parametrize("name", ["scan.tif", "scan.tiff"])
def test_a_tiff_is_read_as_a_tiff_whatever_the_file_holds(tmp_path, monkeypatch, name):
    from PIL import EpsImagePlugin

    from src.processing.proxy import proxy_gen

    ran: list = []

    def ghostscript(*args, **kwargs):
        ran.append(args)
        raise RuntimeError("Ghostscript ran")

    monkeypatch.setattr(EpsImagePlugin, "Ghostscript", ghostscript)
    source = tmp_path / name
    source.write_bytes(encoded("EPS", (64, 48)))
    assert Image.open(source).format == "EPS", "what Pillow makes of it when it's left to guess"

    with pytest.raises(UnidentifiedImageError):
        proxy_gen.generate_proxy_bytes(source)
    assert ran == [], "PostScript is never handed to Ghostscript"


def _declared_png(width: int, height: int) -> bytes:
    """A PNG whose header says width x height, with almost nothing after it."""
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit grey
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(64))) + chunk(b"IEND", b"")


def test_a_picture_declaring_more_than_max_pixels_is_refused_before_it_is_decoded(tmp_path):
    from src.processing.proxy import proxy_gen

    source = tmp_path / "huge.png"
    source.write_bytes(_declared_png(40_000, 40_000))  # 1.6 gigapixels, in 100 bytes

    with pytest.raises(proxy_gen.TooLarge, match="40,000×40,000 pixels"):
        proxy_gen.generate_proxy_bytes(source)


@pytest.mark.parametrize("ext", sorted(RASTER) + [".heic"])
def test_every_format_is_measured_before_it_is_decoded(tmp_path, monkeypatch, ext):
    from src.processing.proxy import proxy_gen

    source = tmp_path / f"photo{ext}"
    source.write_bytes(HEIC.read_bytes() if ext == ".heic" else encoded(RASTER[ext]))
    monkeypatch.setattr(proxy_gen, "MAX_PIXELS", 1000)

    with pytest.raises(proxy_gen.TooLarge):
        proxy_gen.generate_proxy_bytes(source)


def test_a_raws_preview_over_max_pixels_is_passed_over_for_the_raw_itself(tmp_path, monkeypatch):
    from src.processing.proxy import proxy_gen

    source = tmp_path / "IMG_0004.dng"
    source.write_bytes(dng((640, 480), preview=(2100, 1400)))
    monkeypatch.setattr(proxy_gen, "MAX_PIXELS", 1_000_000)  # the sensor's 0.3 MP fits; its 2.9 MP preview doesn't

    proxy, w, h = proxy_gen.generate_proxy_bytes(source)

    assert (w, h) == (640, 480) and proxy_size(proxy) == (640, 480), "demosaiced, not the preview"


def test_a_raw_over_max_pixels_is_refused_before_it_is_demosaiced(tmp_path, monkeypatch):
    from src.processing.proxy import proxy_gen

    source = tmp_path / "IMG_0005.dng"
    source.write_bytes(dng((640, 480)))
    monkeypatch.setattr(proxy_gen, "MAX_PIXELS", 100_000)
    monkeypatch.setattr(proxy_gen, "open_file", lambda *a, **k: pytest.fail("read again by libvips"))

    with pytest.raises(proxy_gen.TooLarge, match="640×480"):
        proxy_gen.generate_proxy_bytes(source)


# ---------------------------------------------------------------------------
# Only trusted loaders

_UNTRUSTED = 16  # VIPS_OPERATION_UNTRUSTED


def _pdf() -> bytes:
    buf = io.BytesIO()
    picture((64, 48)).save(buf, "PDF")
    return buf.getvalue()


UNTRUSTED_CONTENT = {
    "svgload_source": lambda: b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="48"><rect width="64" height="48"/></svg>',
    "pdfload_source": _pdf,
    "magickload_source": lambda: encoded("BMP", (64, 48)),
    "ppmload_source": lambda: b"P6\n4 4\n255\n" + bytes(48),
}


@pytest.mark.parametrize("loader", sorted(UNTRUSTED_CONTENT))
def test_a_file_only_an_untrusted_loader_reads_makes_no_proxy(tmp_path, loader):
    from src.processing.ingest import _jpeg_to_webp
    from src.processing.proxy import proxy_gen

    pyvips = proxy_gen.pyvips
    if not pyvips.type_find("VipsOperation", loader):
        pytest.skip(f"this libvips ({pyvips.version(0)}.{pyvips.version(1)}) has no {loader}")
    if not pyvips.Introspect.get(loader).flags & _UNTRUSTED:
        pytest.skip(f"{loader} is trusted in this libvips ({pyvips.version(0)}.{pyvips.version(1)})")
    content = UNTRUSTED_CONTENT[loader]()
    source = tmp_path / "photo.jpg"  # named as a photo; libvips goes by the content
    source.write_bytes(content)

    with pytest.raises(pyvips.Error):
        proxy_gen.generate_proxy_bytes(source)
    with pytest.raises(pyvips.Error):
        _jpeg_to_webp(content)  # bytes, too


def test_libvips_8_13_or_later_blocks_its_untrusted_loaders(monkeypatch):
    from src.processing import libvips

    block = MagicMock()
    monkeypatch.setattr(libvips.pyvips, "version", lambda i: (8, 13, 0)[i])
    monkeypatch.setattr(libvips.pyvips, "block_untrusted_set", block)

    libvips._block_untrusted()

    block.assert_called_once_with(True)


TOO_OLD = ("libvips 8.12 is too old: Lumiverb reads photos with libvips 8.13 or later, the first that can turn off "
           "the loaders libvips doesn't trust (ImageMagick, PDF, SVG and others). Install a newer libvips (Ubuntu "
           "24.04 or later has one), then restart Lumiverb.")


def test_an_older_libvips_is_refused_saying_which_and_what_it_needs(monkeypatch):
    from src.processing import libvips

    block = MagicMock()
    monkeypatch.setattr(libvips.pyvips, "version", lambda i: (8, 12, 1)[i])
    monkeypatch.setattr(libvips.pyvips, "block_untrusted_set", block)

    with pytest.raises(libvips.TooOld) as refused:
        libvips._block_untrusted()

    block.assert_not_called()
    assert str(refused.value) == TOO_OLD
    assert isinstance(refused.value, ImportError), "libvips can't be imported as Lumiverb uses it"


def test_an_older_libvips_is_refused_wherever_it_comes_in_every_time(monkeypatch):
    """Importing libvips is where it's first used: refused there, and again at every try (a module whose
    import failed isn't kept), so nothing goes on with the untrusted loaders on. A scan brings it in."""
    import sys

    from tests import old_libvips

    old_libvips.pretend(monkeypatch)
    for _ in range(2):
        for name in ("src.processing.libvips", "src.processing.proxy.proxy_gen", "src.processing.scan"):
            with pytest.raises(ImportError) as refused:
                importlib.import_module(name)
            assert str(refused.value) == TOO_OLD
            assert name not in sys.modules


def test_a_stored_preview_is_passed_through_as_it_is_where_libvips_is_refused(monkeypatch):
    """What only shrinks a stored preview for CLIP, faces and the rest (proxy_cache.fit) uses no libvips then, as
    for a picture it can't read: the work from what's stored goes on."""
    from src.processing.proxy.proxy_cache import fit
    from tests import old_libvips

    big = encoded("JPEG", (1600, 1200))
    assert fit(big, 800) != big, "made smaller with a libvips new enough"
    old_libvips.pretend(monkeypatch)
    assert fit(big, 800) == big


def test_the_api_and_the_scheduler_start_and_answer_where_libvips_is_refused(tmp_path):
    """On a machine whose libvips is too old, nothing that starts imports it: the API answers, the scheduler, its
    producers' work, the plug-in runner, the CLI and a device load without it. Only a scan brings it in, and
    is refused."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import sys
        import pyvips
        pyvips.version = lambda i: (8, 12, 1)[i]
        import src.server.api.main
        import src.server.scheduler.service, src.server.scheduler.account, src.server.scheduler.inputs
        import src.server.scheduler.runners, src.server.scheduler.scans, src.server.scheduler.dispatch
        import src.server.plugin_runner, src.client.cli.main, src.processing.device
        from src.producers import load
        from src.shared.producers import PRODUCERS
        for p in PRODUCERS.values():
            if p.run:
                load(p.run)
        print("in:", sorted(m for m in sys.modules if m == "src.processing.libvips" or m.startswith("src.processing.scan")))
        from fastapi.testclient import TestClient
        print("health:", TestClient(src.server.api.main.app).get("/health").status_code)
        try:
            import src.processing.scan
        except ImportError as exc:
            print("scan:", exc)
    """)
    ran = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True,
                         text=True, timeout=300)
    assert ran.returncode == 0, ran.stderr
    lines = ran.stdout.splitlines()
    assert "in: []" in lines and "health: 200" in lines, ran.stdout
    assert f"scan: {TOO_OLD}" in lines, ran.stdout


def test_lumiverb_scan_says_so_plainly(monkeypatch):
    from unittest.mock import patch

    from typer.testing import CliRunner

    from src.client.cli.main import app
    from tests import old_libvips

    old_libvips.pretend(monkeypatch)
    with patch("src.client.cli.main.LumiverbClient") as client:
        result = CliRunner().invoke(app, ["scan", "--library", "L"])
    assert result.exit_code == 1
    assert " ".join(result.output.split()) == f"Can't scan on this machine: {TOO_OLD}", result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), "never a traceback"
    client.assert_not_called()


def test_every_module_using_libvips_gets_it_from_the_one_that_blocks_untrusted_loaders():
    src = Path(__file__).resolve().parents[1] / "src"
    importers = set()
    for path in src.rglob("*.py"):
        if "node_modules" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            if any(n.split(".")[0] == "pyvips" for n in names):
                importers.add(path.relative_to(src.parent).as_posix())

    assert importers == {"src/processing/libvips.py"}
