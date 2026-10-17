# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Scans and content hashes: a file over 64 MiB is known by its fp1 (3 MiB
of it, src/shared/content_hash.py), and every clip hashed before fp1 keeps
what it had.

A clip whose hash was made before fp1 is its whole file's. A scan that finds
it unchanged reads 3 MiB of it and swaps its hash for its fp1 (and its
lineage's), so nothing is made again; until then a file of its size is read
whole too, so a move, a person's trash and a changed file are told as they
were. The real run_scan over real files against the real API and database
(tests/test_scan_outcomes.py's harness); a clip hashed before fp1 is made by
writing its whole file's SHA-256 where its fp1 was, as every clip over 64
MiB had before.
"""

from __future__ import annotations

import io
import json
import struct
import zlib
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from rich.console import Console
from sqlalchemy import text

from src.processing.scan import run_scan
from src.shared.content_hash import FULL_UP_TO, content_hash, sampled, whole_file_sha256
from tests.machine_lineage import ingest_made, made
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_archive_by_hand import _db
from tests.derived import saved
from tests.test_scan_outcomes import (  # noqa: F401 — fixtures
    HOUR,
    Lib,
    _ago,
    _cache_here,
    _clip,
    _image,
    _in_sight,
    _missing,
    _move,
    _note,
    _remove,
    _rows,
    _scan,
    _set_mtime,
    _settled,
    api,
    lib,
)

pytestmark = pytest.mark.slow

SIZE = FULL_UP_TO + 4096


def _big(lib: Lib, rel: str, n: int, *, mtime: float | None = None) -> Path:
    """A PNG whose picture is n's own, over 64 MiB: a private chunk of zeros
    pads it (sparse on disk; previews and EXIF skip it), so every one is the
    same size. Written an hour ago unless said otherwise."""
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (n % 256, (n // 256) % 256, 128)).save(buf, format="PNG", compress_level=0)
    png = buf.getvalue()
    end = png.rindex(b"IEND") - 4
    pad = SIZE - len(png) - 12
    crc, zeros, left = zlib.crc32(b"lvPd"), bytes(1 << 20), pad
    while left:
        crc = zlib.crc32(zeros[:min(left, len(zeros))], crc)
        left -= min(left, len(zeros))
    path = lib.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(png[:end])
        f.write(struct.pack(">I", pad) + b"lvPd")
        f.seek(pad, 1)
        f.write(struct.pack(">I", crc))
        f.write(png[end:])
    assert path.stat().st_size == SIZE
    _set_mtime(path, _ago(HOUR) if mtime is None else mtime)
    return path


@contextmanager
def _whole_reads(lib: Lib):
    """The files a scan reads whole (for a hash made before fp1), by rel path."""
    read: list[str] = []
    root = lib.root.resolve()

    def whole(path):
        read.append(Path(path).resolve().relative_to(root).as_posix())
        return whole_file_sha256(path)

    with (patch("src.processing.scan.whole_file_sha256", side_effect=whole),
          patch("src.processing.ingest.whole_file_sha256", side_effect=whole)):
        yield read


def _hashed_before_fp1(env, asset_id: str, path: Path) -> str:
    """The clip as every clip over 64 MiB was before fp1: known by its whole
    file's SHA-256, and so was everything made from it. Returns that hash."""
    whole = whole_file_sha256(path)
    with _db(env) as session:
        made = session.execute(text(
            "UPDATE artifact_lineage SET source_hash = :w"
            " WHERE asset_id = :a AND source_hash = (SELECT content_hash FROM assets WHERE asset_id = :a)"),
            {"w": whole, "a": asset_id}).rowcount
        session.execute(text("UPDATE assets SET content_hash = :w WHERE asset_id = :a"), {"w": whole, "a": asset_id})
        session.commit()
    assert made > 0  # its preview and capture facts, at least
    return whole


def _sources(env, asset_id: str) -> set[str | None]:
    """What everything made from the clip was made from."""
    with _db(env) as session:
        return set(session.execute(text("SELECT source_hash FROM artifact_lineage WHERE asset_id = :a"),
                                   {"a": asset_id}).scalars())


def _ignored(env, lib: Lib) -> list[str]:
    with _db(env) as session:
        return list(session.execute(text("SELECT content_hash FROM ignored_files WHERE library_id = :lib"),
                                    {"lib": lib.id}).scalars())


def _explain(api, lib: Lib, **options) -> str:
    out = io.StringIO()
    run_scan(api, lib.library, explain=True, concurrency=1, console=Console(file=out, width=200), **options)
    return out.getvalue()


def _group(printed: str, heading: str) -> list[str]:
    """The paths --explain lists under a heading."""
    lines = printed.split("\n")
    start = next(n for n, line in enumerate(lines) if line.startswith(f"  {heading} ("))
    paths = []
    for line in lines[start + 1:]:
        if not line.startswith("    "):
            break
        paths.append(line.strip())
    return paths


def test_a_big_new_file_is_known_by_its_fp1(env, api, lib):
    path = _big(lib, "a.png", 1)
    with _whole_reads(lib) as whole:
        _scan(api, lib)
    clip = _in_sight(env, lib)["a.png"]
    assert clip["content_hash"] == content_hash(path) and sampled(clip["content_hash"])
    assert _sources(env, clip["asset_id"]) == {clip["content_hash"]}
    assert whole == []  # nothing hashed before fp1: no file read whole
    _settled(env, api, lib)


def test_an_unchanged_clip_hashed_before_fp1_takes_its_fp1_and_keeps_everything(env, api, lib):
    path = _big(lib, "a.png", 1)
    _scan(api, lib)
    clip = _in_sight(env, lib)["a.png"]
    _note(env, clip["asset_id"], "keeper")
    whole_hash = _hashed_before_fp1(env, clip["asset_id"], path)
    before = _clip(env, lib, clip["asset_id"])

    with _whole_reads(lib) as whole:
        stats = _scan(api, lib)

    after = _in_sight(env, lib)["a.png"]
    assert after["asset_id"] == clip["asset_id"] and after["note"] == "keeper"
    assert after["content_hash"] == content_hash(path)
    # Everything made from it was made from this content: nothing is made again.
    assert _sources(env, clip["asset_id"]) == {after["content_hash"]} and whole_hash not in _sources(env, clip["asset_id"])
    assert (stats.new, stats.changed, stats.unchanged) == (0, 0, 1)
    assert after["updated_at"] == before["updated_at"]
    assert whole == []  # 3 MiB of it, as it was unchanged
    _settled(env, api, lib)


def test_a_moved_clip_hashed_before_fp1_is_found_by_its_whole_file(env, api, lib):
    path = _big(lib, "a.png", 1)
    _big(lib, "b.png", 2)
    _scan(api, lib)
    clip = _in_sight(env, lib)["a.png"]
    _hashed_before_fp1(env, clip["asset_id"], path)
    _move(lib, "a.png", "moved/a.png")

    with _whole_reads(lib) as whole:
        _scan(api, lib)

    clips = _in_sight(env, lib)
    assert set(clips) == {"moved/a.png", "b.png"}
    assert clips["moved/a.png"]["asset_id"] == clip["asset_id"]
    assert clips["moved/a.png"]["content_hash"] == content_hash(lib.root / "moved/a.png")
    assert _sources(env, clip["asset_id"]) == {clips["moved/a.png"]["content_hash"]}
    assert whole == ["moved/a.png"]  # its size is a clip's hashed before fp1: compared with what was
    assert _missing(env, lib) == {}
    _settled(env, api, lib)


@pytest.mark.parametrize(("how", "heading"), [("trashed", "In the trash"), ("archived", "Archived by a person"),
                                               ("deleted for good", "Deleted for good")])
def test_a_removed_file_hashed_before_fp1_stays_out_when_touched(env, api, lib, how, heading):
    path = _big(lib, "a.png", 1, mtime=_ago(2 * HOUR))
    _big(lib, "b.png", 2)
    _scan(api, lib)
    clip = _in_sight(env, lib)["a.png"]
    whole_hash = _hashed_before_fp1(env, clip["asset_id"], path)
    _remove(env, clip["asset_id"], how)
    _set_mtime(path, _ago(HOUR))  # touched: read again, and still the file removed

    # A dry run tells it's the file removed, and swaps nothing.
    assert _group(_explain(api, lib, include_trash=True), heading) == ["a.png"]
    removed = _clip(env, lib, clip["asset_id"])
    assert (removed["content_hash"] if removed else _ignored(env, lib)[0]) == whole_hash

    with _whole_reads(lib) as whole:
        _scan(api, lib)
    assert set(_in_sight(env, lib)) == {"b.png"}
    assert whole == ["a.png"]
    if how == "deleted for good":
        assert _ignored(env, lib) == [content_hash(path)]
    else:
        assert _clip(env, lib, clip["asset_id"])["content_hash"] == content_hash(path)
    _settled(env, api, lib)


def test_a_missing_clip_hashed_before_fp1_comes_back_at_a_new_path(env, api, lib, tmp_path):
    path = _big(lib, "a.png", 1)
    _big(lib, "b.png", 2)
    _scan(api, lib)
    clip = _in_sight(env, lib)["a.png"]
    _note(env, clip["asset_id"], "keeper")
    _hashed_before_fp1(env, clip["asset_id"], path)
    away = tmp_path / "away.png"
    path.rename(away)
    _scan(api, lib)
    assert set(_missing(env, lib)) == {"a.png"}

    (lib.root / "back").mkdir()
    away.rename(lib.root / "back/a.png")
    with _whole_reads(lib) as whole:
        _scan(api, lib)

    clips = _in_sight(env, lib)
    assert clips["back/a.png"]["asset_id"] == clip["asset_id"] and clips["back/a.png"]["note"] == "keeper"
    assert clips["back/a.png"]["content_hash"] == content_hash(lib.root / "back/a.png")
    assert whole == ["back/a.png"]
    assert _missing(env, lib) == {}
    _settled(env, api, lib)


def test_a_dry_run_swaps_no_hash(env, api, lib):
    path = _big(lib, "a.png", 1)
    _scan(api, lib)
    clip = _in_sight(env, lib)["a.png"]
    whole_hash = _hashed_before_fp1(env, clip["asset_id"], path)
    before = _rows(env, lib)

    stats = _scan(api, lib, dry_run=True)

    assert _rows(env, lib) == before and _sources(env, clip["asset_id"]) == {whole_hash}
    assert (stats.new, stats.changed, stats.unchanged) == (0, 0, 1)
    _scan(api, lib)
    assert _in_sight(env, lib)["a.png"]["content_hash"] == content_hash(path)


def test_another_file_where_a_clip_hashed_before_fp1_was_is_a_new_clip(env, api, lib):
    path = _big(lib, "a.png", 1, mtime=_ago(2 * HOUR))
    _scan(api, lib)
    clip = _in_sight(env, lib)["a.png"]
    whole_hash = _hashed_before_fp1(env, clip["asset_id"], path)
    _big(lib, "a.png", 2)  # other content, the same size

    with _whole_reads(lib) as whole:
        _scan(api, lib)

    now = _in_sight(env, lib)["a.png"]
    assert now["asset_id"] != clip["asset_id"] and now["content_hash"] == content_hash(path)
    old = _clip(env, lib, clip["asset_id"])
    assert old["deleted_reason"] == "missing" and old["content_hash"] == whole_hash  # its content wasn't seen
    assert whole == ["a.png"]
    _settled(env, api, lib)


def test_a_whole_file_hash_comes_only_beside_an_fp1(env, lib):
    client, headers, *_ = env
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(buf, format="JPEG")
    for size, exif in (
        (SIZE, {"content_hash": "sha256:" + "a" * 64, "previous_hash": "sha256:" + "b" * 64}),  # not beside an fp1
        (SIZE, {"content_hash": "fp1:" + "a" * 64, "previous_hash": "fp1:" + "b" * 64}),
        (100, {"content_hash": "fp1:" + "a" * 64, "previous_hash": "sha256:" + "b" * 64}),  # no fp1 up to 64 MiB
        (SIZE, {"content_hash": "fp1:" + "a" * 64, "previous_hash": "b" * 64}),  # a bare hash names no scheme
        (100, {"content_hash": "a" * 64}),
        (100, {"content_hash": "md5:" + "a" * 32}),  # not a scheme this server takes
    ):
        data = {"library_id": lib.id, "rel_path": "x.jpg", "file_size": str(size), "media_type": "image",
                "exif": json.dumps(exif), "lineage": ingest_made(exif["content_hash"])}
        buf.seek(0)
        r = client.post("/v1/ingest", data=data, files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=headers)
        assert r.status_code == 400, (exif, r.text)
    assert _rows(env, lib) == []

    r = client.post(f"/v1/libraries/{lib.id}/scans", json={}, headers=headers)
    assert r.status_code == 200, r.text
    scan_id = r.json()["scan_id"]
    for bad in ({"content_hash": "fp1:" + "a" * 63}, {"content_hash": "a" * 64}, {"content_hash": "SHA256:" + "a" * 64},
                {"content_hash": "fp1:" + "a" * 64, "previous_hash": "fp1:" + "b" * 64},
                {"content_hash": "fp1:" + "a" * 64, "previous_hash": "b" * 64}):
        r = client.post(f"/v1/scans/{scan_id}/hashes", json={"files": [{"rel_path": "x.png", **bad}]}, headers=headers)
        assert r.status_code == 422, (bad, r.text)


def test_work_handed_out_before_the_swap_is_of_this_content(env, api, lib):
    # A producer handed the clip before its hash was swapped saves with the
    # whole file's hash: that's the content its fp1 names, so nothing is stale.
    client, headers, *_ = env
    path = _big(lib, "a.png", 1)
    _scan(api, lib)
    clip = _in_sight(env, lib)["a.png"]
    whole_hash = _hashed_before_fp1(env, clip["asset_id"], path)
    _scan(api, lib)  # swapped

    r = saved(client, "ocr", clip["asset_id"], json={"ocr_text": "", "lineage": made("ocr", whole_hash)},
              headers=headers)
    assert r.status_code == 200, r.text
    assert _sources(env, clip["asset_id"]) == {content_hash(path)}

    # A clip up to 64 MiB is known by its whole file's hash: the source it's given is kept.
    small = _image(lib, "small.png", 3)
    _scan(api, lib)
    other = _in_sight(env, lib)["small.png"]
    r = saved(client, "ocr", other["asset_id"], json={"ocr_text": "", "lineage": made("ocr", whole_hash)},
              headers=headers)
    assert r.status_code == 200, r.text
    with _db(env) as session:
        source = session.execute(text("SELECT source_hash FROM artifact_lineage WHERE asset_id = :a"
                                      " AND artifact = 'ocr'"), {"a": other["asset_id"]}).scalar_one()
    assert source == whole_hash and other["content_hash"] == whole_file_sha256(small)
