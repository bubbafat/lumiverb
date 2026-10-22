"""A test run leaves nothing in the checkout (tests/left_behind.py, tests/conftest.py): what's new at its end
fails it, named, and the server's DATA_DIR, which defaults into the checkout, is each test process's own folder.
The run's own hooks are tried in runs of their own, in a checkout of their own (tmp_path)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import left_behind

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]


def _make(root: Path, *paths: str) -> None:
    for rel in paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")


def _new(root: Path, before: frozenset[str]) -> list[str]:
    return left_behind.left(root, before, left_behind.paths(root))


# -- What's new --------------------------------------------------------------------------


def test_what_is_new_is_named_a_new_folder_once_with_what_is_in_it(tmp_path):
    _make(tmp_path, "src/a.py", "data/ten_old/lib/p.webp")
    before = left_behind.paths(tmp_path)
    _make(tmp_path, "data/ten_new/lib/p.webp", "lumiverb.egg-info/PKG-INFO", "left.txt", "src/b.log")
    assert _new(tmp_path, before) == ["data/ten_new/ (and 2 in it)", "left.txt", "lumiverb.egg-info/ (and 1 in it)",
                                      "src/b.log"]
    assert _new(tmp_path, left_behind.paths(tmp_path)) == []


def test_what_runs_the_tests_makes_is_not_read_and_a_virtualenv_only_when_new(tmp_path):
    _make(tmp_path, "src/a.py", ".venv/lib/site.py", "src/ui/web/node_modules/react/index.js", "clients/README.md",
          "tools/README.md")
    before = left_behind.paths(tmp_path)
    _make(tmp_path, "__pycache__/x.pyc", "src/__pycache__/a.cpython-312.pyc", ".pytest_cache/v/cache/lastfailed",
          ".claude/worktrees/agent-1/data/ten_x/p.webp", ".git/index.lock", ".DS_Store", "src/.DS_Store",
          ".ruff_cache/0.15/123", ".mypy_cache/3.12/x.json",
          ".venv/lib/new.py", "src/ui/web/node_modules/vite/index.js")
    assert _new(tmp_path, before) == []
    _make(tmp_path, "clients/node_modules/x.js", "tools/.venv/bin/python")
    assert _new(tmp_path, before) == ["clients/node_modules/", "tools/.venv/"]


def test_the_summary_names_twenty_at_most():
    lines = [f"data/ten_{i:02}/" for i in range(25)]
    assert left_behind.summary(lines[:20]) == lines[:20]
    assert left_behind.summary(lines) == [*lines[:20], "… and 5 more"]


# -- The run's own hooks, in a checkout of their own -------------------------------------------------


def _run(checkout: Path, tests: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    """pytest with this suite's conftest (its hooks and settings), on the tests given, in a checkout of their own."""
    (checkout / "pytest.ini").write_text("[pytest]\n")
    for name, code in tests.items():
        (checkout / name).write_text(code)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST")}
    # This suite's tests importable; no Postgres started for a run that has no database tests.
    env.update(PYTHONPATH=str(ROOT), LUMIVERB_TEST_PG_PER_MODULE="1")
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "tests.conftest", "-q", *args, *tests],
                          cwd=checkout, env=env, capture_output=True, text=True, timeout=300)


LEAVES_A_FILE = """
from pathlib import Path

def test_it():
    Path("left.txt").write_text("a test's")
"""

LEAVES_A_FOLDER = """
from pathlib import Path

def test_it():
    (Path("data") / "ten_x").mkdir(parents=True)
    (Path("data") / "ten_x" / "p.webp").write_bytes(b"")
"""

# The server's DATA_DIR, written as the API writes it, and said (to a folder outside the checkout) to be looked at.
USES_DATA_DIR = """
import os
from pathlib import Path

from src.server.config import Settings

def test_it():
    data = Path(Settings().data_dir)
    (data / "ten_x" / "lib").mkdir(parents=True, exist_ok=True)
    (data / "ten_x" / "lib" / "p.webp").write_bytes(b"")
    (Path(os.environ["SAY_TO"]) / os.environ.get("PYTEST_XDIST_WORKER", "main")).write_text(str(data.resolve()))
"""


def _said(out: subprocess.CompletedProcess) -> str:
    return out.stdout + out.stderr


def test_a_run_that_leaves_something_in_the_checkout_fails_naming_it(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    out = _run(checkout, {"test_file.py": LEAVES_A_FILE, "test_folder.py": LEAVES_A_FOLDER})
    assert out.returncode == 1, _said(out)
    assert "= left in the checkout =" in out.stdout, _said(out)
    assert "  data/ (and 2 in it)\n" in out.stdout and "  left.txt\n" in out.stdout, _said(out)
    stats = out.stdout.splitlines()[-1]
    assert "2 passed" in stats and "2 left in the checkout" in stats, stats
    log = (checkout / ".pytest_cache" / "failures.log").read_text()
    assert "LEFT IN THE CHECKOUT" in log and "data/ (and 2 in it)" in log and "left.txt" in log


def test_under_xdist_the_controller_names_what_its_workers_left(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    out = _run(checkout, {"test_file.py": LEAVES_A_FILE, "test_folder.py": LEAVES_A_FOLDER}, "-n", "2")
    assert out.returncode == 1, _said(out)
    assert "  data/ (and 2 in it)\n" in out.stdout and "  left.txt\n" in out.stdout, _said(out)
    stats = out.stdout.splitlines()[-1]
    assert "2 passed" in stats and "2 left in the checkout" in stats, stats


def test_a_clean_run_passes_and_the_servers_data_dir_is_each_processs_own_and_gone_after(tmp_path, monkeypatch):
    checkout, said = tmp_path / "checkout", tmp_path / "said"
    checkout.mkdir()
    said.mkdir()
    monkeypatch.setenv("SAY_TO", str(said))
    out = _run(checkout, {"test_data_dir.py": USES_DATA_DIR}, "-n", "2", "--dist", "each")  # on both workers
    assert out.returncode == 0, _said(out)
    assert "left in the checkout" not in out.stdout, _said(out)
    folders = {Path(p.read_text()) for p in said.iterdir()}
    assert len(folders) == 2, f"one each, not one they share: {folders}"
    for folder in folders:
        assert not folder.is_relative_to(checkout) and not folder.exists(), folder


def test_here_too_data_dir_is_out_of_the_checkout_and_its_models_are_this_machines():
    from src.processing import models_dir
    from src.processing.workers.faces.insightface_provider import model_root
    from src.server.config import Settings
    from src.server.lifecycle.cli import data_dir

    for where in (Path(Settings().data_dir), data_dir(dict(os.environ))):
        assert where.is_absolute() and not where.resolve().is_relative_to(ROOT), where
    data = Path(os.environ["DATA_DIR"])
    # Weights downloaded once, never into each process's DATA_DIR (and an operator's folder: never moved or pruned).
    assert not model_root().is_relative_to(data)
    assert not Path(os.environ["HF_HOME"]).is_relative_to(data) and models_dir.hf_home().is_relative_to(data)


def test_a_uv_run_a_test_starts_runs_what_is_installed():
    """scripts/migrate.sh's `uv run`, run by tests/test_own_plugins.py: syncing would build the project into the
    checkout (lumiverb.egg-info) and install it into the virtualenv these tests run with."""
    assert os.environ["UV_NO_SYNC"] == "1"
