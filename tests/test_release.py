"""Releasing (scripts/release.sh): the formula's release set by
scripts/bump_formula.py, added the first time and replaced after, only the
formula's own url and sha256; the advisories in what it ships listed by
scripts/audit_release.py; and the script refuses what it can't release."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("bump_formula", SCRIPTS / "bump_formula.py")
bump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)
audit_spec = importlib.util.spec_from_file_location("audit_release", SCRIPTS / "audit_release.py")
audit = importlib.util.module_from_spec(audit_spec)
sys.modules[audit_spec.name] = audit  # its dataclass looks itself up there
audit_spec.loader.exec_module(audit)

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


@pytest.mark.parametrize("args", [["v0.1.1", "--yes"], ["v0.1.1", "--dry-run", "--force"]])
def test_release_refuses_an_option_it_doesnt_know(args):
    done = subprocess.run(["bash", str(SCRIPTS / "release.sh"), *args], capture_output=True, text=True)
    assert done.returncode == 1 and "Not an option: --" in done.stderr


def test_the_audit_reads_the_extras_the_formula_installs():
    formula = """  def install
    system "uv", "sync", "--frozen",
           "--python", Formula["python@3.12"].opt_bin/"python3.12",
           "--extra", "cli", "--extra", "embeddings", "--extra", "face_recognition", "--extra", "workers",
           "--no-dev"
"""
    assert audit.formula_extras(formula) == ["cli", "embeddings", "face_recognition", "workers"]


def test_each_python_package_is_listed_once_with_what_fixes_it():
    report = {"dependencies": [
        {"name": "pyjwt", "version": "2.12.1", "vulns": [
            {"id": "PYSEC-1", "fix_versions": ["2.14.0"]}, {"id": "PYSEC-2", "fix_versions": ["2.15.0"]},
            {"id": "PYSEC-1", "fix_versions": ["2.14.0"]}]},  # a package locked on two lines is reported twice
        {"name": "torch", "version": "2.10.0", "vulns": [
            {"id": "PYSEC-3", "fix_versions": []}, {"id": "PYSEC-4", "fix_versions": ["2.13.0"]}]},
        {"name": "onnx", "version": "1.21.0", "vulns": [{"id": "PYSEC-5", "fix_versions": []}]},
        {"name": "fine", "version": "1.0", "vulns": []},
    ]}
    assert [f.line() for f in audit.python_findings(report)] == [
        "python onnx 1.21.0: PYSEC-5; no fix yet",
        "python pyjwt 2.12.1: PYSEC-1, PYSEC-2; fixed by 2.15.0",
        "python torch 2.10.0: PYSEC-3, PYSEC-4; fixed by 2.13.0 (1 with no fix yet)",
    ]


def test_npm_packages_are_listed_for_their_own_advisories():
    report = {"vulnerabilities": {
        "react-router": {"range": "6.0.0 - 7.17.0", "fixAvailable": {
            "name": "react-router-dom", "version": "7.18.4", "isSemVerMajor": True},
            "via": [{"url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc"}]},
        "react-router-dom": {"range": "6.0.0 - 7.17.0", "fixAvailable": True, "via": ["react-router"]},
        "vite": {"range": "8.0.0 - 8.0.15", "fixAvailable": True,
                 "via": [{"url": "https://github.com/advisories/GHSA-dddd-eeee-ffff"}]},
    }}
    assert [f.line() for f in audit.npm_findings(report)] == [
        "npm react-router 6.0.0 - 7.17.0: GHSA-aaaa-bbbb-cccc; fixed by react-router-dom 7.18.4 (a major upgrade)",
        "npm vite 8.0.0 - 8.0.15: GHSA-dddd-eeee-ffff; fixed by npm audit fix",
    ]


def test_an_audit_that_cant_run_is_never_taken_for_advisories(tmp_path):
    formula = tmp_path / "lumiverb.rb"
    formula.write_text('"--extra", "cli"')
    done = subprocess.run([sys.executable, str(SCRIPTS / "audit_release.py"), "no-such-commit", str(formula)],
                          capture_output=True, text=True, cwd=SCRIPTS.parent)
    assert done.returncode not in (0, audit.FOUND)
    assert "The audit couldn't run" in done.stderr
