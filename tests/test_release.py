"""Releasing (scripts/release.sh): the formula's release set by
scripts/bump_formula.py, added the first time and replaced after, only the
formula's own url and sha256; and the script refuses what it can't release."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("bump_formula", SCRIPTS / "bump_formula.py")
bump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)

SHA_1, SHA_2 = "a" * 64, "b" * 64
URL_1 = "https://github.com/bubbafat/lumiverb/archive/refs/tags/v0.1.0.tar.gz"
URL_2 = "https://github.com/bubbafat/lumiverb/archive/refs/tags/v0.1.1.tar.gz"

HEAD_ONLY = '''class Lumiverb < Formula
  desc "Photo and video library server"
  homepage "https://github.com/bubbafat/lumiverb"
  license "MIT"
  head "https://github.com/bubbafat/lumiverb.git", branch: "main"

  depends_on "node" => :build

  resource "something" do
    url "https://example.com/something.tar.gz"
    sha256 "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  end
end
'''


def test_the_first_release_comes_after_the_homepage_and_head_stays():
    formula = bump.bumped(HEAD_ONLY, URL_1, SHA_1)
    assert f'  homepage "https://github.com/bubbafat/lumiverb"\n  url "{URL_1}"\n  sha256 "{SHA_1}"\n  license' in formula
    assert 'head "https://github.com/bubbafat/lumiverb.git", branch: "main"' in formula


def test_a_later_one_replaces_it_and_nothing_else():
    first = bump.bumped(HEAD_ONLY, URL_1, SHA_1)
    second = bump.bumped(first, URL_2, SHA_2)
    assert second == first.replace(URL_1, URL_2).replace(SHA_1, SHA_2)
    assert 'url "https://example.com/something.tar.gz"' in second, "a resource's own is left alone"


@pytest.mark.parametrize(("url", "sha", "says"), [
    (URL_1, "not-a-sha", "Not a sha256"),
    ('https://x" + system("rm") + "', SHA_1, "Not a url"),
])
def test_what_isn_t_a_release_is_refused(url, sha, says):
    with pytest.raises(bump.FormulaError, match=says):
        bump.bumped(HEAD_ONLY, url, sha)


def test_a_formula_it_doesn_t_recognise_is_left_to_a_human():
    with pytest.raises(bump.FormulaError, match="not both"):
        bump.bumped(HEAD_ONLY.replace("  license", f'  url "{URL_1}"\n  license'), URL_2, SHA_2)


def test_the_command_edits_the_file(tmp_path):
    formula = tmp_path / "lumiverb.rb"
    formula.write_text(HEAD_ONLY)
    assert bump.main([str(formula), URL_1, SHA_1]) == 0
    assert f'url "{URL_1}"' in formula.read_text()


@pytest.mark.parametrize("args", [[], ["0.1.1"], ["v0.1"], ["latest"]])
def test_release_wants_a_version_like_v1_2_3(args):
    done = subprocess.run(["bash", str(SCRIPTS / "release.sh"), *args], capture_output=True, text=True)
    assert done.returncode == 1 and "Say the version: scripts/release.sh vX.Y.Z" in done.stderr
