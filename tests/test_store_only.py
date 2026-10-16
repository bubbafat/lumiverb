"""Everything Lumiverb keeps is reached through the Store (ADR-020 phase 2):
outside src/server/storage/ no code takes a stored file's path from the
filesystem provider (abs_path, write_path, paths) or holds the provider
itself (LocalStorage, get_storage). A stored file a tool needs as a path
comes from store.local_file or store.writing, for that block.

Read as source: a call through an alias or a getattr would get past it,
and a reviewer would see it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PROVIDER = SRC / "server" / "storage"
FORBIDDEN = re.compile(r"\.(abs_path|write_path)\(|\bget_storage\(|\bLocalStorage\b|storage\.local import"
                       r"|\b(?:storage|store)\.paths\(")


def _python_outside_the_provider() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if PROVIDER not in p.parents and "__pycache__" not in p.parts]


def test_nothing_outside_the_storage_package_takes_a_stored_files_path():
    found = []
    for path in _python_outside_the_provider():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line.split("#", 1)[0]):
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert found == []


def test_the_check_would_find_one():
    for line in ("p = storage.abs_path(key)", "storage.write_path(key).write_bytes(b)",
                 "from src.server.storage.local import get_storage", "s = LocalStorage(data)",
                 "for p in storage.paths(key):"):
        assert FORBIDDEN.search(line), line
    for line in ("store.local_file(key)", "def get_storage_location(request):", "store.write(key, data)"):
        assert not FORBIDDEN.search(line), line
