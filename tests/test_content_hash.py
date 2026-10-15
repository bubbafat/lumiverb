"""A file's content hash (src/shared/content_hash.py).

fp1 is a durable format: every clip over 64 MiB is known by it, so its tag,
sample sizes, offsets and threshold never change. These pin it with a value
worked out once, and show what it sees and what it can't.
"""

from __future__ import annotations

import builtins
import hashlib
import io
from pathlib import Path

import pytest

from src.shared import content_hash as ch

pytestmark = pytest.mark.fast

BIG = ch.FULL_UP_TO + 12345


def _marked(path: Path, size: int) -> Path:
    """A file of `size` zero bytes (sparse: nothing written but the marks)
    with a byte of its own at each place fp1 reads: its head's first and
    last, each sample's first, its tail's first, and its last."""
    with open(path, "wb") as f:
        f.truncate(size)
        for n, offset in enumerate([0, ch.HEAD - 1, *ch.sample_offsets(size), size - ch.TAIL, size - 1], start=1):
            f.seek(offset)
            f.write(bytes([n]))
    return path


def _poke(path: Path, offset: int, value: int = 0xFF) -> None:
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(bytes([value]))


def test_the_format_is_pinned(tmp_path):
    assert (ch.FULL_UP_TO, ch.HEAD, ch.TAIL, ch.SAMPLES, ch.SAMPLE, ch.TAG, ch.PREFIX) == (
        64 * 1024 * 1024, 1024 * 1024, 1024 * 1024, 16, 64 * 1024, b"lumiverb-fp1", "fp1:")
    assert ch.sample_offsets(BIG) == [
        1048576, 5379144, 9709712, 14040280, 18370848, 22701416, 27031984, 31362552,
        35693120, 40023688, 44354256, 48684824, 53015392, 57345960, 61676528, 66007097]
    assert ch.content_hash(_marked(tmp_path / "big", BIG)) == (
        "fp1:aca5f347a349094a90d06f523153f5f38a72dc359bd7f14d0b3768258468ee57")


def test_up_to_64_mib_a_file_is_known_by_its_whole_sha256(tmp_path):
    small = tmp_path / "small"
    small.write_bytes(b"hello world")
    assert ch.content_hash(small) == hashlib.sha256(b"hello world").hexdigest()

    edge = _marked(tmp_path / "edge", ch.FULL_UP_TO)
    assert ch.content_hash(edge) == hashlib.sha256(edge.read_bytes()).hexdigest()
    assert not ch.sampled(ch.content_hash(edge))


def test_a_bigger_file_is_read_3_mib_whatever_its_size(tmp_path, monkeypatch):
    big = _marked(tmp_path / "big", ch.FULL_UP_TO * 3)
    read = []
    real_open = builtins.open

    class Counted:
        """The file, counting what's read of it."""

        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def read(self, n=-1):
            out = self.f.read(n)
            read.append(len(out))
            return out

        def seek(self, offset):
            return self.f.seek(offset)

        def fileno(self):
            return self.f.fileno()

    def counted_open(path, mode="r", *args, **kwargs):
        return Counted(real_open(path, mode, *args, **kwargs))

    monkeypatch.setattr(ch, "open", counted_open, raising=False)
    assert ch.sampled(ch.content_hash(big))
    assert sum(read) == ch.HEAD + ch.TAIL + ch.SAMPLES * ch.SAMPLE == 3 * 1024 * 1024


def test_what_fp1_sees(tmp_path):
    def poked(name: str, offset: int) -> str:
        path = _marked(tmp_path / name, BIG)
        _poke(path, offset)
        return ch.content_hash(path)

    before = ch.content_hash(_marked(tmp_path / "a", BIG))
    assert poked("head", 10) != before
    assert poked("sample", ch.sample_offsets(BIG)[7] + 100) != before
    assert poked("tail", BIG - 10) != before
    longer = _marked(tmp_path / "longer", BIG + 1)
    assert ch.content_hash(longer) != before  # its size is hashed


def test_what_fp1_cant_see_is_an_edit_between_its_samples(tmp_path):
    # The known limit: the same size, a change no sample reads.
    before = ch.content_hash(_marked(tmp_path / "a", BIG))
    between = ch.sample_offsets(BIG)[3] + ch.SAMPLE + 1000
    path = _marked(tmp_path / "b", BIG)
    _poke(path, between)
    assert ch.content_hash(path) == before


def test_a_file_cut_short_while_hashed_cant_be_hashed():
    with pytest.raises(OSError):
        ch._read_at(io.BytesIO(b"short"), 0, 10)


def test_a_hash_made_before_fp1_is_still_its_files(tmp_path):
    big = _marked(tmp_path / "big", BIG)
    whole = ch.whole_file_sha256(big)
    assert whole == hashlib.sha256(big.read_bytes()).hexdigest()
    assert ch.whole_file(whole, BIG)
    assert ch.matches(big, whole)
    assert ch.matches(big, ch.content_hash(big))
    assert not ch.matches(big, "0" * 64)
    assert not ch.matches(big, "fp1:" + "0" * 64)

    small = tmp_path / "small"
    small.write_bytes(b"x")
    sha = hashlib.sha256(b"x").hexdigest()
    assert not ch.whole_file(sha, 1)  # a small file's hash is its whole file's, now as then
    assert ch.matches(small, sha)


def test_telling_the_kinds_apart():
    sha = "a" * 64
    assert ch.sampled("fp1:" + sha) and not ch.sampled(sha) and not ch.sampled(None)
    assert ch.whole_file(sha, ch.FULL_UP_TO + 1)
    assert not ch.whole_file(sha, ch.FULL_UP_TO)
    assert not ch.whole_file("fp1:" + sha, ch.FULL_UP_TO + 1)
    assert not ch.whole_file(None, ch.FULL_UP_TO + 1) and not ch.whole_file(sha, None)
