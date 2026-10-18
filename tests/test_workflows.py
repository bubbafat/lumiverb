"""GitHub's workflows (.github/workflows/): every action is pinned to a
commit, never a tag or branch that could be moved to other code, and the
dependency audit runs the release's own audit (scripts/audit_release.py)
on what changes what's installed and every week."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _workflows() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def test_every_action_is_pinned_to_a_commit():
    found = [(p.name, use) for p in _workflows() for use in USES.findall(p.read_text())]
    assert found, "no workflow uses an action: is the folder there?"
    assert [f for f in found if not PINNED.match(f[1])] == []


def test_the_check_would_find_one():
    for use in ("actions/checkout@v7", "actions/checkout@main", "astral-sh/setup-uv@v10.2.0",
                "actions/checkout@3d3c42e"):
        assert not PINNED.match(use), use
    assert PINNED.match("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1")


def test_the_dependency_audit_is_the_releases_own():
    text = (WORKFLOWS / "dependency-audit.yml").read_text()
    assert "python3 scripts/audit_release.py HEAD" in text
    for locked in ("uv.lock", "pyproject.toml", "src/ui/web/package-lock.json", "src/ui/web/package.json"):
        assert text.count(f"- {locked}") == 2, locked  # pull requests and main
    assert "schedule:" in text and "cron:" in text
    assert "permissions:\n  contents: read" in text
