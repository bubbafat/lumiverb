"""Scanning what changed, and each library in full once a day (ADR-016 phase 4).

The scheduler looks at each library every 30 seconds: it scans the folders
the storage machine reported changed (and each library in full once a day),
then acknowledges what the scan saw. Libraries whose storage is asleep
aren't scanned; probing and rendering wait for them. (Moved from the
worker's tests: the scheduler took over its scanning.)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from src.client.cli import roots
from src.client.cli.config import CLIConfig, save_config
from src.client.cli.scan import ScanStats
from src.server.scheduler.scans import (
    Retry,
    ScanState,
    ServiceLock,
    load_state,
    save_state,
    saved_form,
    scan_pass,
    scan_scope,
)

MAC = "/Volumes/media-01"
HOUR = 3600.0
MIN = 60.0


# ---------------------------------------------------------------------------
# scan_scope
# ---------------------------------------------------------------------------


def _dirs(*names: str):
    return lambda rel: rel in names


@pytest.mark.fast
def test_a_changed_file_scans_its_folder() -> None:
    assert scan_scope(["Day 1/A001.mov"], _dirs("Day 1")) == "Day 1"


@pytest.mark.fast
def test_a_changed_folder_scans_itself() -> None:
    assert scan_scope(["Day 1/Cam A"], _dirs("Day 1", "Day 1/Cam A")) == "Day 1/Cam A"


@pytest.mark.fast
def test_changes_in_two_folders_scan_their_common_folder() -> None:
    # One scan, so a file moved between them is seen as a move.
    paths = ["Shoots/Day 1/A.mov", "Shoots/Day 2/B.mov"]
    assert scan_scope(paths, _dirs("Shoots", "Shoots/Day 1", "Shoots/Day 2")) == "Shoots"


@pytest.mark.fast
def test_a_removed_folder_scans_its_parent() -> None:
    assert scan_scope(["Shoots/Gone"], _dirs("Shoots")) == "Shoots"


@pytest.mark.fast
def test_a_change_at_the_top_scans_everything() -> None:
    assert scan_scope(["A.mov"], _dirs()) is None
    assert scan_scope(["", "Day 1/A.mov"], _dirs("Day 1")) is None


@pytest.mark.fast
def test_folders_only_share_whole_names() -> None:
    assert scan_scope(["Day 1/a.mov", "Day 10/b.mov"], _dirs("Day 1", "Day 10")) is None


# ---------------------------------------------------------------------------
# scan_pass
# ---------------------------------------------------------------------------


@dataclass
class FakeServer:
    libraries: list[dict]
    pending: dict[str, list[dict]] = field(default_factory=dict)
    truncated: set[str] = field(default_factory=set)
    acks: list[tuple[str, list[dict]]] = field(default_factory=list)

    def client(self) -> MagicMock:
        client = MagicMock()

        def get(url: str, **kw):
            resp = MagicMock()
            if url.endswith("/changes"):
                lib = url.split("/")[3]
                resp.json.return_value = {"changes": self.pending.get(lib, []), "truncated": lib in self.truncated}
            else:
                raise AssertionError(url)
            return resp

        def post(url: str, **kw):
            if url.endswith("/changes/ack"):
                self.acks.append((url.split("/")[3], kw["json"]["changes"]))
            return MagicMock()

        client.get.side_effect = get
        client.post.side_effect = post
        return client


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", lambda: h)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    return h


@pytest.fixture
def das(home: Path, tmp_path: Path) -> Path:
    mount = tmp_path / "mnt"
    (mount / "Footage" / "Day 1").mkdir(parents=True)
    (mount / "Footage" / "Day 1" / "A001.mov").write_bytes(b"x")
    save_config(CLIConfig(root_map={MAC: str(mount)}))
    return mount


LIB = {"library_id": "lib_1", "name": "Footage", "root_path": f"{MAC}/Footage"}
CHANGE = {"change_id": "chg_1", "rel_path": "Day 1/A001.mov", "reported_at": "2026-10-07T00:00:00Z", "version": 7}


def _pass(server: FakeServer, state: ScanState | None = None, *, now: float = 100 * HOUR, **kw):
    scan = kw.pop("scan", MagicMock(return_value=ScanStats()))
    state = state or ScanState(last_full_scan={"lib_1": now - 1})
    roots = scan_pass(server.client(), server.libraries, state, now=now, scan_fn=scan,
                      console=Console(quiet=True), **kw)
    return scan, state, {k: v is not None for k, v in roots.items()}


@pytest.mark.fast
def test_reported_changes_are_scanned_then_acknowledged(das: Path) -> None:
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    scan, _, reachable = _pass(server)
    [call] = scan.call_args_list
    assert call.kwargs["path_prefix"] == "Day 1"
    assert call.kwargs["allow_moves"] is True
    # The archive model: missing files are archived (reversible) on a healthy
    # mount, so no guard holds them back (Robert's call, Oct 8).
    assert call.kwargs["allow_mass_delete"] is True
    assert server.acks == [("lib_1", [{"change_id": "chg_1", "version": 7}])]
    assert reachable == {"lib_1": True}


@pytest.mark.fast
def test_where_each_librarys_storage_is_is_said_before_any_scan(das: Path) -> None:
    # Review round 2: probes and renders waited for the whole first pass (a
    # full scan of a big library) to learn where the storage is.
    seen: list[dict] = []
    scan = MagicMock(side_effect=lambda *a, **kw: (seen.append(dict(said)), ScanStats())[1])
    said: dict = {}
    server = FakeServer([LIB, {"library_id": "lib_2", "name": "Gone", "root_path": "/nowhere"}],
                        pending={"lib_1": [CHANGE]})
    _pass(server, scan=scan, on_roots=said.update)
    assert seen and seen[0] == {"lib_1": das / "Footage", "lib_2": None}


@pytest.mark.fast
def test_a_folder_named_in_nfd_on_disk_is_scanned_itself(das: Path) -> None:
    import unicodedata

    zurich = unicodedata.normalize("NFC", "Zürich")
    (das / "Footage" / unicodedata.normalize("NFD", zurich) / "Cam A").mkdir(parents=True)
    server = FakeServer([LIB], pending={"lib_1": [{**CHANGE, "rel_path": f"{zurich}/Cam A"}]})
    scan, _, _ = _pass(server)
    assert scan.call_args.kwargs["path_prefix"] == f"{zurich}/Cam A"


@pytest.mark.fast
def test_a_changed_folder_that_cannot_be_checked_is_scanned_from_its_parent(das: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    # Its parent's scan covers it whether it's a folder or a file.
    import errno
    import os

    (das / "Footage" / "Day 1" / "Cam A").mkdir()
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if os.fspath(path).endswith("Cam A"):
            raise OSError(errno.EIO, "Input/output error", os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    server = FakeServer([LIB], pending={"lib_1": [{**CHANGE, "rel_path": "Day 1/Cam A"}]})
    scan, _, _ = _pass(server)
    assert scan.call_args.kwargs["path_prefix"] == "Day 1"


@pytest.mark.fast
def test_nothing_reported_and_no_full_scan_due_means_no_scan(das: Path) -> None:
    scan, _, _ = _pass(FakeServer([LIB]))
    scan.assert_not_called()


@pytest.mark.fast
def test_a_full_scan_runs_when_due_and_is_remembered(das: Path) -> None:
    state = ScanState(last_full_scan={"lib_1": 0.0})
    scan, state, _ = _pass(FakeServer([LIB]), state, now=30 * HOUR, full_scan_every=24 * HOUR)
    assert scan.call_args.kwargs["path_prefix"] is None
    assert state.last_full_scan["lib_1"] == 30 * HOUR


@pytest.mark.fast
def test_the_first_pass_scans_every_library_in_full(das: Path) -> None:
    scan, _, _ = _pass(FakeServer([LIB]), ScanState())
    assert scan.call_args.kwargs["path_prefix"] is None


@pytest.mark.fast
def test_a_truncated_backlog_scans_everything(das: Path) -> None:
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]}, truncated={"lib_1"})
    scan, _, _ = _pass(server)
    assert scan.call_args.kwargs["path_prefix"] is None
    assert server.acks == [("lib_1", [{"change_id": "chg_1", "version": 7}])]


@pytest.mark.fast
def test_sleeping_storage_is_not_scanned_and_says_so(home: Path) -> None:
    save_config(CLIConfig(root_map={MAC: str(home / "not-mounted")}))
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    scan, _, reachable = _pass(server, ScanState())
    scan.assert_not_called()
    assert server.acks == []
    assert reachable == {"lib_1": False}


@pytest.mark.fast
def test_an_empty_mount_point_is_never_scanned(home: Path, tmp_path: Path) -> None:
    # An unmounted share is an empty folder; scanning it would mark every
    # file missing.
    mount = tmp_path / "empty-mount"
    (mount / "Footage").mkdir(parents=True)
    save_config(CLIConfig(root_map={MAC: str(mount)}))
    scan, _, reachable = _pass(FakeServer([LIB]), ScanState())
    scan.assert_not_called()
    assert reachable == {"lib_1": False}


@pytest.mark.fast
def test_a_failed_scan_keeps_the_changes(das: Path) -> None:
    server = FakeServer([LIB, {**LIB, "library_id": "lib_2", "name": "Two"}],
                        pending={"lib_1": [CHANGE], "lib_2": [{**CHANGE, "change_id": "chg_2"}]})
    scan = MagicMock(side_effect=[RuntimeError("server hiccup"), ScanStats()])
    _pass(server, ScanState(last_full_scan={"lib_1": 99 * HOUR, "lib_2": 99 * HOUR}), scan=scan)
    assert [lib for lib, _ in server.acks] == ["lib_2"]


@pytest.mark.fast
def test_a_scan_that_lost_its_root_keeps_the_changes(das: Path) -> None:
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _pass(server, scan=MagicMock(return_value=ScanStats(root_unreachable=True)))
    assert server.acks == []


def _failing(*paths: str) -> MagicMock:
    return MagicMock(return_value=ScanStats(failed=len(paths), failed_paths=list(paths)))


@pytest.mark.fast
def test_a_file_that_fails_does_not_hold_up_the_changes(das: Path) -> None:
    # Kept, every new report would widen the scan toward the whole library,
    # retrying the bad file every minute. It gets its own retry instead.
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, state, _ = _pass(server, scan=_failing("Day 1/A001.mov"))
    assert server.acks == [("lib_1", [{"change_id": "chg_1", "version": 7}])]
    assert state.retries["lib_1"].paths == {"Day 1/A001.mov"}


@pytest.mark.fast
def test_files_still_being_written_keep_the_changes(das: Path) -> None:
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _pass(server, scan=MagicMock(return_value=ScanStats(settling=1, settling_paths=["Day 1/A001.mov"])))
    assert server.acks == []


@pytest.mark.fast
def test_only_changes_covering_a_file_still_being_written_wait(das: Path) -> None:
    changes = [CHANGE, {**CHANGE, "change_id": "chg_2", "rel_path": "Day 1/A002.mov"},
               {**CHANGE, "change_id": "chg_3", "rel_path": "Day 1"}]
    server = FakeServer([LIB], pending={"lib_1": changes})
    _pass(server, scan=MagicMock(return_value=ScanStats(settling=1, settling_paths=["Day 1/A001.mov"])))
    assert server.acks == [("lib_1", [{"change_id": "chg_2", "version": 7}])]


# A file that always fails is retried with back-off, not every look.


@pytest.mark.fast
def test_a_failed_file_is_tried_again_with_back_off(das: Path) -> None:
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, state, _ = _pass(server, now=t, scan=_failing("Day 1/A001.mov"))
    server.pending = {}
    tries = []
    for minute in range(1, 40):
        scan, state, _ = _pass(server, state, now=t + minute * MIN, scan=_failing("Day 1/A001.mov"))
        if scan.called:
            assert scan.call_args.kwargs["path_prefix"] == "Day 1"
            tries.append(minute)
    assert tries == [5, 15, 35]


@pytest.mark.fast
def test_back_off_stops_growing_at_a_day(das: Path) -> None:
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    state = ScanState(last_full_scan={"lib_1": t})
    _pass(server, state, now=t, scan=_failing("Day 1/A001.mov"), full_scan_every=1e9)
    server.pending = {}
    for _ in range(12):
        _pass(server, state, now=state.retries["lib_1"].due, scan=_failing("Day 1/A001.mov"), full_scan_every=1e9)
    assert state.retries["lib_1"].delay == 24 * HOUR


@pytest.mark.fast
def test_a_clean_scan_of_the_folder_ends_the_retries(das: Path) -> None:
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, state, _ = _pass(server, now=t, scan=_failing("Day 1/A001.mov"))
    # The storage machine reports the file again (fixed, say) before the retry is due.
    _, state, _ = _pass(server, state, now=t + MIN)
    assert state.retries == {}
    server.pending = {}
    scan, _, _ = _pass(server, state, now=t + HOUR)
    scan.assert_not_called()


@pytest.mark.fast
def test_a_retry_not_yet_due_does_not_widen_other_scans(das: Path) -> None:
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, state, _ = _pass(server, now=t, scan=_failing("Day 1/A001.mov"))
    server.pending = {"lib_1": [{**CHANGE, "change_id": "chg_2", "rel_path": "Day 2/B001.mov"}]}
    scan, state, _ = _pass(server, state, now=t + MIN)
    assert scan.call_args.kwargs["path_prefix"] == "Day 2"
    assert state.retries["lib_1"].paths == {"Day 1/A001.mov"}


# A folder that couldn't be listed: its changes wait, with back-off.


@pytest.mark.fast
def test_a_scan_that_could_not_list_a_folder_keeps_the_changes(das: Path) -> None:
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _pass(server, scan=MagicMock(return_value=ScanStats(unlisted=["Day 1"])))
    assert server.acks == []


@pytest.mark.fast
def test_changes_under_a_folder_that_could_not_be_listed_wait_for_the_retry(das: Path) -> None:
    # A folder that stays unreadable (chmod 000) mustn't be rescanned every look.
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, state, _ = _pass(server, now=t, scan=MagicMock(return_value=ScanStats(unlisted=["Day 1"])))
    scan, state, _ = _pass(server, state, now=t + MIN)
    scan.assert_not_called()
    scan, state, _ = _pass(server, state, now=t + 5 * MIN)
    assert scan.call_args.kwargs["path_prefix"] == "Day 1"
    assert server.acks == [("lib_1", [{"change_id": "chg_1", "version": 7}])]
    assert state.retries == {}


@pytest.mark.fast
def test_a_change_reported_again_does_not_wait_for_the_retry(das: Path) -> None:
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, state, _ = _pass(server, now=t, scan=MagicMock(return_value=ScanStats(unlisted=["Day 1"])))
    server.pending = {"lib_1": [{**CHANGE, "version": 8}]}
    scan, _, _ = _pass(server, state, now=t + MIN)
    assert scan.call_args.kwargs["path_prefix"] == "Day 1"
    assert server.acks == [("lib_1", [{"change_id": "chg_1", "version": 8}])]


# ---------------------------------------------------------------------------
# What survives a restart
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_full_scan_times_and_retries_survive_a_restart(tmp_path: Path) -> None:
    # Otherwise every deploy scans every library in full, and files waiting
    # for a retry wait for the next full scan instead.
    state = ScanState(last_full_scan={"lib_1": 123.0},
                      retries={"lib_1": Retry({"Day 1/A001.mov", "Locked"}, 600.0, 1234.5, {"chg_1": 7})})
    path = tmp_path / "worker-state.json"
    save_state(path, saved_form(state))
    again = load_state(path)
    assert again.last_full_scan == {"lib_1": 123.0}
    assert again.retries == state.retries


@pytest.mark.fast
@pytest.mark.parametrize("content", ["not json", '["a list"]', '{"last_full_scan": {"lib_1": "soon"}}',
                                     '{"last_full_scan": {}, "retries": {"lib_1": {"paths": 3}}}'])
def test_an_unreadable_state_file_means_full_scans(tmp_path: Path, content: str) -> None:
    path = tmp_path / "worker-state.json"
    path.write_text(content)
    assert load_state(path).last_full_scan == {}


@pytest.mark.fast
def test_a_missing_state_file_means_full_scans(tmp_path: Path) -> None:
    assert load_state(tmp_path / "never-saved.json").last_full_scan == {}


# ---------------------------------------------------------------------------
# Process safety
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_only_one_scheduler_runs_per_machine(tmp_path: Path) -> None:
    first = ServiceLock(tmp_path / "worker.lock")
    second = ServiceLock(tmp_path / "worker.lock")
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


@pytest.mark.fast
def test_the_lock_is_the_old_workers(home: Path) -> None:
    # A worker left running and the scheduler never both run.
    lock = ServiceLock()
    assert lock.acquire()
    try:
        assert lock._path.name == "worker.lock"
    finally:
        lock.release()


@pytest.mark.fast
def test_a_hung_probe_is_not_piled_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Each look probes each root; a mount that hangs must not leave a new
    # stuck thread behind every 30 seconds.
    release = threading.Event()
    calls: list[Path] = []

    def hang(path: Path, require_entries: bool) -> Path:
        calls.append(path)
        release.wait(5)
        return path

    monkeypatch.setattr(roots, "_probe", hang)
    lib = {"library_id": "lib_h", "root_path": str(tmp_path)}
    try:
        assert roots.reachable_root(lib, root_map={}, timeout=0.05) is None
        assert roots.reachable_root(lib, root_map={}, timeout=0.05) is None
        assert len(calls) == 1
    finally:
        release.set()


# ---------------------------------------------------------------------------
# Reporting changes (the storage machine's side)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_report_changes_sends_paths_as_libraries_store_them(home: Path, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    from typer.testing import CliRunner

    main = importlib.import_module("src.client.cli.main")
    save_config(CLIConfig(root_map={MAC: "/mnt/media-01"}))
    client = MagicMock()
    client.post.return_value.json.return_value = {
        "accepted": 1, "libraries": {"lib_1": 1}, "unmatched": 1, "unmatched_sample": ["/elsewhere/x.mov"],
    }
    monkeypatch.setattr(main, "LumiverbClient", lambda: client)
    result = CliRunner().invoke(main.app, ["library", "report-changes",
                                           "/mnt/media-01/Footage/Day 1/A001.mov", "/elsewhere/x.mov"])
    assert result.exit_code == 0, result.output
    client.post.assert_called_once_with("/v1/changes", json={
        "paths": [f"{MAC}/Footage/Day 1/A001.mov", "/elsewhere/x.mov"],
    })
    assert "1 change" in result.output and "1 path" in result.output


@pytest.mark.fast
def test_report_changes_reads_paths_from_stdin(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    from typer.testing import CliRunner

    main = importlib.import_module("src.client.cli.main")
    client = MagicMock()
    client.post.return_value.json.return_value = {"accepted": 2, "libraries": {"lib_1": 2}, "unmatched": 0,
                                                   "unmatched_sample": []}
    monkeypatch.setattr(main, "LumiverbClient", lambda: client)
    result = CliRunner().invoke(main.app, ["library", "report-changes", "--stdin"],
                                input="/Volumes/a/x.mov\n\n/Volumes/a/y.mov\n")
    assert result.exit_code == 0, result.output
    assert client.post.call_args.kwargs["json"]["paths"] == ["/Volumes/a/x.mov", "/Volumes/a/y.mov"]


@pytest.mark.fast
def test_report_changes_makes_relative_paths_absolute(home: Path, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    from typer.testing import CliRunner

    main = importlib.import_module("src.client.cli.main")
    client = MagicMock()
    client.post.return_value.json.return_value = {"accepted": 1, "libraries": {}, "unmatched": 0,
                                                   "unmatched_sample": []}
    monkeypatch.setattr(main, "LumiverbClient", lambda: client)
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(main.app, ["library", "report-changes", "clip.mov"])
    assert client.post.call_args.kwargs["json"]["paths"] == [str(tmp_path / "clip.mov")]
