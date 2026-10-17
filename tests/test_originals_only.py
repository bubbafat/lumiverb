"""A library's originals are read only through their interface (ADR-020
decision 5, src/processing/originals.py): outside it no code in src finds a
library's folder on this machine (roots.reachable_root, local_library_root)
or builds a path to a file in it (io_utils.resolve_source_path). So every
read of an original, the scheduler's and the API's as well as the CLI's
scan, is a reach and then a walk, a stat or a local_file block. roots.py and
io_utils.py, which the interface is built on, define them.

Read as source: a call through an alias or a getattr would get past it,
and a reviewer would see it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ALLOWED = {SRC / "processing" / "originals.py", SRC / "processing" / "roots.py", SRC / "shared" / "io_utils.py"}
FORBIDDEN = re.compile(r"\b(reachable_root|local_library_root|resolve_source_path|_find_source_path)\b")


def _python_outside_the_interface() -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py")) if p not in ALLOWED and "__pycache__" not in p.parts]


def test_nothing_outside_the_interface_finds_an_original():
    found = []
    for path in _python_outside_the_interface():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line.split("#", 1)[0]):
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert found == []


def test_the_check_would_find_one():
    for line in ("root = reachable_root(library, require_entries=True)",
                 "from src.processing.roots import local_library_root",
                 "path = resolve_source_path(root, rel_path)"):
        assert FORBIDDEN.search(line), line
    for line in ("originals = reach(library, require_entries=True)", "with originals.local_file(rel) as path:",
                 "here = where(library)", "st = originals.stat(rel)"):
        assert not FORBIDDEN.search(line), line
