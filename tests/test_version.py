"""Which Lumiverb is running (src/server/version.py), for the Admin page: the
package's own version isn't moved at a release, so it's read from the
install. A Homebrew keg's folder names it; a checkout asks git; else
"unknown", never a guess."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.server import version

pytestmark = pytest.mark.fast


@pytest.fixture(autouse=True)
def _fresh():
    version.running.cache_clear()
    yield
    version.running.cache_clear()


def test_a_homebrew_keg_names_it(monkeypatch):
    monkeypatch.setattr(version, "ROOT", Path("/opt/homebrew/Cellar/lumiverb/0.1.6/libexec"))
    assert version.running() == "0.1.6"


def test_a_checkout_says_what_git_says(tmp_path, monkeypatch):
    # This machine's git config is none of the test's business (no identity
    # on the Linux box; a signing setting on someone's Mac).
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(key, value)

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "one")
    git("tag", "-a", "v0.1.6", "-m", "v0.1.6")
    monkeypatch.setattr(version, "ROOT", tmp_path)
    assert version.running() == "v0.1.6"
    version.running.cache_clear()
    git("commit", "-q", "--allow-empty", "-m", "two")
    assert version.running().startswith("v0.1.6-1-g")


def test_anything_else_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "ROOT", tmp_path)  # no keg, no checkout
    assert version.running() == "unknown"

