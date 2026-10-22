"""A file's content hash: what Lumiverb knows a file's content by (ADR-016).

Moves, a person's trash, a file changed in place and what each artifact was
made from (lineage) are all decided by comparing content hashes. Every one
says how it was made, so two are compared only when made the same way:

    content-hash = scheme ":" value
    scheme       = 1*32( a-z / 0-9 )         lowercase letters and digits
    value        = 1*128( A-Z / a-z / 0-9 / "." / "_" / "~" / "-" )

The value is the scheme's to define (never ":" or a space; SHA-512 in hex
fits). Two hashes name
the same content only when both scheme and value are equal; hashes of
different schemes are never compared. The server's own schemes are fixed:

- "sha256": the SHA-256 of every byte, 64 lowercase hex digits. A file of
  FULL_UP_TO bytes or fewer is hashed this way;
- "fp1": a bigger file, the SHA-256 of 3 MiB of it (_fp1), 64 lowercase hex
  digits, whatever its size: reading a whole 100 GB clip to know it is as
  costly as copying it.

They are the only ones the API takes for now (SERVER: a bare hash, without
its scheme, is refused rather than guessed). fp1's tag, sample sizes,
offsets and threshold never change (tests/test_content_hash.py pins them);
another way of hashing is another scheme. What fp1 can't see: an edit
between its samples that leaves the size as it was. Camera originals and
renders aren't edited that way; a re-render of the same length changes the
bytes the samples read.

A clip hashed before fp1, of a file over FULL_UP_TO, has its whole file's
"sha256" (`whole_file`): the server swaps it for the file's fp1 as scans
meet it (src/server/scans.py `rekey`), and until then a file compared with
it is read whole too.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import BinaryIO

# Files this size or smaller are hashed whole.
FULL_UP_TO = 64 * 1024 * 1024

# The server's schemes, with the ":" that ends them.
SHA256 = "sha256:"
FP1 = "fp1:"

# fp1: SHA-256 over TAG, the size as 8 bytes big-endian, the first HEAD
# bytes, SAMPLES samples of SAMPLE bytes spread between them, and the last
# TAIL bytes.
TAG = b"lumiverb-fp1"
HEAD = TAIL = 1024 * 1024
SAMPLES = 16
SAMPLE = 64 * 1024

_CHUNK = 1024 * 1024
# Any content hash (the grammar above), and the ones the server makes and takes.
GRAMMAR = r"^[a-z0-9]{1,32}:[A-Za-z0-9._~-]{1,128}$"
PATTERN = r"^(sha256|fp1):[0-9a-f]{64}$"
_GRAMMAR = re.compile(GRAMMAR)
_SERVER = re.compile(PATTERN)


def well_formed(value: str | None) -> bool:
    """Whether value is a content hash of any scheme (the grammar)."""
    return bool(value) and bool(_GRAMMAR.fullmatch(value))


def ours(value: str | None) -> bool:
    """Whether value is one of the server's schemes, exactly as it makes them."""
    return bool(value) and bool(_SERVER.fullmatch(value))


def scheme(value: str | None) -> str | None:
    """The scheme a content hash names ("sha256", "fp1"), or None when it isn't one."""
    return value.split(":", 1)[0] if well_formed(value) else None


def sampled(value: str | None) -> bool:
    """An fp1 (a bigger file's sampled hash)."""
    return bool(value) and value.startswith(FP1)


def whole_file(value: str | None, size: int | None) -> bool:
    """A hash made before fp1 of a file fp1 hashes now: its whole file's
    SHA-256 (the server swaps it for the fp1 as scans meet the file)."""
    return bool(value) and size is not None and size > FULL_UP_TO and value.startswith(SHA256)


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
    return FP1 + h.hexdigest()


def _whole(f: BinaryIO) -> str:
    h = hashlib.sha256()
    f.seek(0)
    while piece := f.read(_CHUNK):
        h.update(piece)
    return SHA256 + h.hexdigest()


def _size(f: BinaryIO) -> int:
    return os.fstat(f.fileno()).st_size


def content_hash(path: Path) -> str:
    """The file's content hash (OSError when it can't be read)."""
    with open(path, "rb") as f:
        size = _size(f)
        return _fp1(f, size) if size > FULL_UP_TO else _whole(f)


def whole_file_sha256(path: Path) -> str:
    """The file's "sha256" hash, every byte read: to compare with a hash made before fp1."""
    with open(path, "rb") as f:
        return _whole(f)


def matches(path: Path, expected: str) -> bool:
    """Whether the file's content is the one `expected` names, hashed the way
    it was made (a hash made before fp1 by reading the whole file). A scheme
    the server doesn't make never matches."""
    with open(path, "rb") as f:
        size = _size(f)
        if whole_file(expected, size):
            return _whole(f) == expected
        return (_fp1(f, size) if size > FULL_UP_TO else _whole(f)) == expected
