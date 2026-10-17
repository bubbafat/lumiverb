"""A job's scratch (ADR-020 decision 4): the one place a producer's work
files go (src/producers/runner.py Work.scratch), gone when the job ends,
however it ends, and at the scheduler's start. No producer makes a temporary
file of its own.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.producers import runner
from src.server.scheduler.dispatch import Job

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    """The scheduler's scratch root, the test's own."""
    root = tmp_path / runner.SCRATCH
    monkeypatch.setattr(runner, "scratch_root", lambda: root)
    return root


@pytest.fixture
def acct() -> MagicMock:
    acct = MagicMock()
    acct.stopping = threading.Event()
    return acct


def _job(*outcomes: str) -> Job:
    return Job("ten_1", "test", 3, tuple({"asset_id": f"ast_{n}", "rel_path": f"{n}.jpg", "outcome": o}
                                         for n, o in enumerate(outcomes)))


class Writes(runner.Work):
    """Writes a work file in its scratch for each clip, then does what the clip says."""

    artifact = "test"
    seen: list[Path] = []

    def make(self, clip: dict) -> Path:
        path = self.scratch / f"{clip['asset_id']}.bin"
        path.write_bytes(b"work")
        Writes.seen.append(path)
        outcome = clip["outcome"]
        if outcome == "failed":
            raise runner.Failed("its own")
        if outcome == "waits":
            raise runner.Waits("later")
        if outcome == "crashes":
            raise OSError(5, "Input/output error")  # this machine's: a crash
        if outcome == "not tried":
            raise runner.NotTried("its storage went away")
        if outcome == "stopped":
            self.acct.stopping.set()
            raise runner.Stopped(clip["asset_id"])
        return path

    def save(self, client, made) -> None:
        for _clip, path in made:
            assert path.read_bytes() == b"work", "what make wrote is there to save"


@pytest.mark.parametrize("outcome", ["saved", "failed", "waits", "crashes", "not tried", "stopped"])
def test_a_job_s_scratch_goes_however_the_job_ends(root, acct, outcome):
    Writes.seen = []
    if outcome == "crashes":
        with pytest.raises(runner.Crashed):
            runner.run(Writes, acct, _job(outcome))
    else:
        runner.run(Writes, acct, _job(outcome))
    [path] = Writes.seen
    assert path.parent.parent == root
    assert not path.parent.exists()
    assert list(root.iterdir()) == []


def test_a_job_s_clips_share_its_scratch_and_each_job_has_its_own(root, acct):
    Writes.seen = []
    runner.run(Writes, acct, _job("saved", "saved"))
    runner.run(Writes, acct, _job("saved"))
    first, second, third = Writes.seen
    assert first.parent == second.parent != third.parent
    assert first.parent.name.startswith("test-")


def test_a_job_that_needs_no_scratch_makes_none(root, acct):
    class Quiet(runner.Work):
        artifact = "quiet"

        def make(self, clip: dict) -> dict:
            return {}

        def save(self, client, made) -> None:
            pass

    assert runner.run(Quiet, acct, _job("saved")) is None
    assert not root.exists()


def test_a_scratch_folder_goes_when_its_block_ends(root, acct):
    work = Writes(acct, _job("saved"))
    with work.scratch_folder() as folder:
        (folder / "audio.wav").write_bytes(b"wav")
        assert folder.parent == work.scratch
    assert not folder.exists() and work.scratch.is_dir()
    work.end()
    assert not root.exists() or list(root.iterdir()) == []


def test_the_scratch_a_killed_scheduler_left_goes_at_its_start(root):
    (root / "test-abc" / "frames").mkdir(parents=True)
    (root / "test-abc" / "frames" / "1000.jpg").write_bytes(b"jpg")
    (root / "stray.tmp").write_bytes(b"x")
    assert runner.empty_scratch() == 2
    assert list(root.iterdir()) == []
    assert runner.empty_scratch() == 0


def test_with_no_scratch_yet_there_s_nothing_to_empty(root):
    assert runner.empty_scratch() == 0


def test_scratch_is_in_the_scheduler_s_temp_folder(monkeypatch, tmp_path):
    """TMPDIR is an install's DATA_DIR/worker-tmp (lumiverb-server run, the
    systemd unit): on the data disk, emptied as the scheduler starts."""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert runner.scratch_root() == tmp_path / runner.SCRATCH


def test_the_scheduler_empties_scratch_as_it_starts():
    """main() empties it once it holds the machine's lock, beside the other
    leftovers it clears: no job is running yet."""
    import inspect

    from src.server.scheduler import service

    source = inspect.getsource(service.main)
    assert source.index("lock.acquire()") < source.index("empty_scratch()")


TEMPORARY = re.compile(r"\btempfile\b|TemporaryDirectory|NamedTemporaryFile|\bmkdtemp\b|\bmkstemp\b")


def test_no_producer_makes_a_temporary_file_of_its_own():
    """Every work file is in the job's scratch: only the runner makes it."""
    found = []
    for path in sorted((ROOT / "src" / "producers").rglob("*.py")):
        if path.name == "runner.py" and path.parent.name == "producers":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if TEMPORARY.search(line.split("#", 1)[0]):
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert found == []


def test_the_check_would_find_one():
    for line in ("import tempfile", "with tempfile.TemporaryDirectory() as tmp:", "from tempfile import mkstemp",
                 "NamedTemporaryFile(suffix='.jpg')"):
        assert TEMPORARY.search(line), line
    assert not TEMPORARY.search("frame = self.scratch / 'frame-1.jpg'")
