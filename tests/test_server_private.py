"""What a Mac install keeps is its user's alone (src/server/lifecycle/private.py).

lumiverb-server run makes everything with umask 077, and at each start takes
group and others' access away from what an earlier install left open: DATA_DIR,
each account's folder in a previews folder chosen elsewhere (not the chosen
folder itself), and the service log with its rotated files.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from src.server.lifecycle import private

pytestmark = pytest.mark.fast


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def install(tmp_path: Path) -> dict[str, Path]:
    """An install as an earlier version left it: everything 0755/0644."""
    data = tmp_path / "data"
    (data / "cache").mkdir(parents=True)
    chosen = tmp_path / "drive"  # a previews folder chosen elsewhere, its accounts' folders in it
    (chosen / "ten_one" / "lib_1" / "proxies").mkdir(parents=True)
    (chosen / "other-things").mkdir()
    library_own = tmp_path / "library-previews"
    (library_own / "ten_one").mkdir(parents=True)
    (data / "storage.json").write_text(json.dumps({"previews": str(chosen),
                                                   "libraries": {"lib_2": str(library_own)}}))
    logs = tmp_path / "log"
    logs.mkdir()
    for name in ("lumiverb.log", "lumiverb.log.1", "lumiverb.log.4", "other.log"):
        (logs / name).write_text("x")
    for path in [data, data / "cache", chosen, chosen / "ten_one", chosen / "other-things", library_own,
                 library_own / "ten_one", logs]:
        path.chmod(0o755)
    for path in logs.iterdir():
        path.chmod(0o644)
    return {"data": data, "chosen": chosen, "library_own": library_own, "log": logs / "lumiverb.log"}


def test_what_an_earlier_install_left_open_is_made_private(install):
    old = os.umask(0o022)
    try:
        private.begin(install["data"], install["log"])
        made_now = install["data"] / "made-after"
        made_now.write_text("x")
        assert _mode(made_now) == 0o600  # umask 077 from here on
    finally:
        os.umask(old)
    assert _mode(install["data"]) == 0o700
    assert _mode(install["chosen"] / "ten_one") == 0o700
    assert _mode(install["library_own"] / "ten_one") == 0o700
    logs = install["log"].parent
    assert [_mode(logs / n) for n in ("lumiverb.log", "lumiverb.log.1", "lumiverb.log.4")] == [0o600] * 3


def test_a_chosen_folder_itself_and_what_isnt_lumiverbs_are_left_alone(install):
    private.make_private(private.paths_to_keep_private(install["data"], install["log"]))
    assert _mode(install["chosen"]) == 0o755  # it may be a drive's own folder
    assert _mode(install["chosen"] / "other-things") == 0o755
    assert _mode(install["library_own"]) == 0o755
    assert _mode(install["log"].parent / "other.log") == 0o644
    assert _mode(install["data"] / "cache") == 0o755  # inside DATA_DIR, which is private now


def test_only_what_this_user_owns_is_changed(install, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    assert private.make_private(private.paths_to_keep_private(install["data"], install["log"])) == []
    assert _mode(install["data"]) == 0o755


def test_a_previews_folder_that_isnt_there_is_skipped(install, tmp_path):
    (install["data"] / "storage.json").write_text(json.dumps({"previews": str(tmp_path / "unplugged")}))
    paths = private.paths_to_keep_private(install["data"], None)
    assert paths == [install["data"]]
    assert [p for p, _, _ in private.make_private([*paths, tmp_path / "gone"])] == [install["data"]]


def test_run_makes_it_private_before_anything_starts():
    source = (Path(__file__).resolve().parents[1] / "src" / "server" / "lifecycle" / "cli.py").read_text()
    run = source[source.index("def _run("):source.index("def _env_option(")]
    assert run.index("private.begin(") < run.index("make_cluster_if_missing(") < run.index("Supervisor(")
