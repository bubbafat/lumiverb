"""A file's content hash: what Lumiverb knows a file's content by (ADR-016).

Moves, a person's trash, a file changed in place and what each artifact was
made from (lineage) are all decided by comparing content hashes. Reading a
whole 100 GB clip to know it is as costly as copying it, so:

- a file of FULL_UP_TO bytes or fewer: the SHA-256 of every byte, as 64 hex
  digits (what every hash was before fp1, so none of those changed);
- a bigger one: "fp1:" and the SHA-256 of 3 MiB of it (FP1), whatever its size.

fp1 is a fixed format: its tag, sample sizes, offsets and threshold never
change (tests/test_content_hash.py pins them); another scheme gets another
prefix. What it can't see: an edit between its samples that leaves the size
as it was. Camera originals and renders aren't edited that way; a re-render
of the same length changes the bytes the samples read.

A hash made before fp1 of a file over FULL_UP_TO is the whole file's
(`whole_file`): the server swaps it for the file's fp1 as scans meet it
(src/server/scans.py `rekey`), and until then a file compared with it is
read whole too.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import BinaryIO

# Files this size or smaller are hashed whole.
FULL_UP_TO = 64 * 1024 * 1024
PREFIX = "fp1:"

# fp1: SHA-256 over TAG, the size as 8 bytes big-endian, the first HEAD
# bytes, SAMPLES samples of SAMPLE bytes spread between them, and the last
# TAIL bytes.
TAG = b"lumiverb-fp1"
HEAD = TAIL = 1024 * 1024
SAMPLES = 16
SAMPLE = 64 * 1024

_CHUNK = 1024 * 1024
# What a content hash looks like: a whole file's SHA-256, or an fp1.
PATTERN = r"^(fp1:)?[0-9a-f]{64}$"
_WHOLE = re.compile(r"^[0-9a-f]{64}$")


def sampled(value: str | None) -> bool:
    """An fp1 (a bigger file's sampled hash)."""
    return bool(value) and value.startswith(PREFIX)


def whole_file(value: str | None, size: int | None) -> bool:
    """A hash made before fp1 of a file fp1 hashes now: its whole file's
    SHA-256 (the server swaps it for the fp1 as scans meet the file)."""
    return bool(value) and size is not None and size > FULL_UP_TO and bool(_WHOLE.match(value))


def sample_offsets(size: int) -> list[int]:
    """Where fp1 reads its samples in a file of `size` bytes (> FULL_UP_TO)."""
    span = size - HEAD - TAIL - SAMPLE
    return [HEAD + i * span // (SAMPLES - 1) for i in range(SAMPLES)]


def _read_at(f: BinaryIO, offset: int, length: int) -> bytes:
    """Exactly `length` bytes at `offset`: a file cut short while read is an OSError."""
    f.seek(offset)
    out = f.read(length)
    if len(out) != length:
        raise OSError(f"file ended at {offset + len(out)} bytes while it was hashed")
    return out


def _fp1(f: BinaryIO, size: int) -> str:
    h = hashlib.sha256(TAG + size.to_bytes(8, "big"))
    h.update(_read_at(f, 0, HEAD))
    for offset in sample_offsets(size):
        h.update(_read_at(f, offset, SAMPLE))
    h.update(_read_at(f, size - TAIL, TAIL))
    return PREFIX + h.hexdigest()


def _whole(f: BinaryIO) -> str:
    h = hashlib.sha256()
    f.seek(0)
    while piece := f.read(_CHUNK):
        h.update(piece)
    return h.hexdigest()


def _size(f: BinaryIO) -> int:
    return os.fstat(f.fileno()).st_size


def content_hash(path: Path) -> str:
    """The file's content hash (OSError when it can't be read)."""
    with open(path, "rb") as f:
        size = _size(f)
        return _fp1(f, size) if size > FULL_UP_TO else _whole(f)


def whole_file_sha256(path: Path) -> str:
    """The SHA-256 of every byte: to compare with a hash made before fp1."""
    with open(path, "rb") as f:
        return _whole(f)


def matches(path: Path, expected: str) -> bool:
    """Whether the file's content is the one `expected` names, hashed the way
    it was made (a hash made before fp1 by reading the whole file)."""
    with open(path, "rb") as f:
        size = _size(f)
        if whole_file(expected, size):
            return _whole(f) == expected
        return (_fp1(f, size) if size > FULL_UP_TO else _whole(f)) == expected
