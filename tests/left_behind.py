"""Nothing a test run makes stays in the checkout (tests/conftest.py checks every run).

What's in the checkout is read as the run starts and again as it ends (by the xdist controller, before its
workers start and after they've finished, or by the run itself without -n). A file or folder that wasn't
there before fails the run, named, git's ignored ones too: a data/ of previews and plug-ins nobody sees, a
build's lumiverb.egg-info. A test writes under its tmp_path, and the server's DATA_DIR is a folder of the test
process's own (conftest).

Not read: git's own, .claude/ (other sessions' worktrees, made while tests run), Python's bytecode, pytest's
and the linters' caches (a lint run while tests run) and Finder's .DS_Store; nor what's inside a virtualenv or
node_modules (only whether one is new).
"""

from __future__ import annotations

import os
from pathlib import Path

# Never read: made by what runs the tests, or by others while they run, never by a test.
NOT_READ = frozenset({".git", ".claude", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".DS_Store"})
# Read as one entry: whether one is new, not what changes inside (the packages the tests run with).
NOT_OPENED = frozenset({".venv", "node_modules"})
# Named at most this many in the run's summary.
NAMED = 20


def paths(root: Path) -> frozenset[str]:
    """Every file and folder under root, as relative paths with "/", but for NOT_READ and what's inside NOT_OPENED."""
    found: set[str] = set()
    folders = [""]
    while folders:
        rel = folders.pop()
        try:
            with os.scandir(root / rel) as listing:
                entries = list(listing)
        except OSError:  # gone meanwhile, or not ours to read
            continue
        for entry in entries:
            if entry.name in NOT_READ:
                continue
            path = f"{rel}/{entry.name}" if rel else entry.name
            found.add(path)
            if entry.name not in NOT_OPENED and entry.is_dir(follow_symlinks=False):
                folders.append(path)
    return frozenset(found)


def left(root: Path, before: frozenset[str], after: frozenset[str]) -> list[str]:
    """What's new, one line each: a new folder once, with how much is in it ("data/ (and 1,234 in it)")."""
    new = after - before
    inside: dict[str, int] = {}
    for path in new:
        top = path
        while (parent := top.rpartition("/")[0]) in new:
            top = parent
        inside[top] = inside.get(top, 0) + (top != path)
    lines = []
    for top in sorted(inside):
        if (root / top).is_dir():
            lines.append(f"{top}/ (and {inside[top]:,} in it)" if inside[top] else f"{top}/")
        else:
            lines.append(top)
    return lines


def summary(lines: list[str]) -> list[str]:
    """The lines the run's summary names, NAMED at most."""
    shown = lines[:NAMED]
    if len(lines) > NAMED:
        shown.append(f"… and {len(lines) - NAMED:,} more")
    return shown
