"""The brain's worker: scan what changed, enrich what's missing (ADR-016 phase 2).

`lumiverb worker` runs as a service on the brain. Each cycle it scans the
folders the Mac reported changed (and each library in full once a day),
then enriches. Libraries whose storage is asleep aren't scanned, but their
videos keep being enriched from analysis proxies.
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
from src.client.cli.worker import WorkerLock, WorkerState, run_cycle, scan_scope

MAC = "/Volumes/media-01"
HOUR = 3600.0


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
# run_cycle
# ---------------------------------------------------------------------------


@dataclass
class FakeServer:
    libraries: list[dict]
    pending: dict[str, list[dict]] = field(default_factory=dict)
    truncated: set[str] = field(default_factory=set)
    summaries: dict[str, dict] = field(default_factory=dict)
    acks: list[tuple[str, list[dict]]] = field(default_factory=list)

    def client(self) -> MagicMock:
        client = MagicMock()

        def get(url: str, **kw):
            resp = MagicMock()
            if url == "/v1/libraries":
                resp.json.return_value = self.libraries
            elif url == "/v1/changes":
                resp.json.return_value = {"libraries": [
                    {"library_id": k, "pending": len(v), "oldest_reported_at": "2026-10-07T00:00:00Z"}
                    for k, v in self.pending.items() if v
                ]}
            elif url.endswith("/changes"):
                lib = url.split("/")[3]
                resp.json.return_value = {"changes": self.pending.get(lib, []), "truncated": lib in self.truncated}
            elif url == "/v1/assets/repair-summary":
                resp.json.return_value = self.summaries.get(kw["params"]["library_id"], {"total_assets": 0})
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


@pytest.fixture(autouse=True)
def ai_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.worker._vision_ready", lambda client: True)
    monkeypatch.setattr("src.client.cli.worker._transcripts_ready", lambda client: True)


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
WORK = {"total_assets": 1, "missing_transcription": 1}


def _cycle(server: FakeServer, state: WorkerState | None = None, *, now: float = 100 * HOUR, **kw):
    scan = kw.pop("scan", MagicMock(return_value=ScanStats()))
    enrich = kw.pop("enrich", MagicMock())
    state = state or WorkerState(last_full_scan={"lib_1": now - 1})
    run_cycle(server.client(), state=state, now=now, scan_fn=scan, enrich_fn=enrich,
              console=Console(quiet=True), **kw)
    return scan, enrich, state


@pytest.mark.fast
def test_reported_changes_are_scanned_then_acknowledged(das: Path) -> None:
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    scan, _, _ = _cycle(server)
    [call] = scan.call_args_list
    assert call.kwargs["path_prefix"] == "Day 1"
    assert call.kwargs["allow_moves"] is True
    # The archive model: missing files are archived (reversible) on a healthy
    # mount, so no 5% guard holds them back (Robert's call, Oct 8).
    assert call.kwargs["allow_mass_delete"] is True
    assert server.acks == [("lib_1", [{"change_id": "chg_1", "version": 7}])]


@pytest.mark.fast
def test_a_folder_named_in_nfd_on_disk_is_scanned_itself(das: Path) -> None:
    import unicodedata

    zurich = unicodedata.normalize("NFC", "Zürich")
    (das / "Footage" / unicodedata.normalize("NFD", zurich) / "Cam A").mkdir(parents=True)
    server = FakeServer([LIB], pending={"lib_1": [{**CHANGE, "rel_path": f"{zurich}/Cam A"}]})
    scan, _, _ = _cycle(server)
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
    scan, _, _ = _cycle(server)
    assert scan.call_args.kwargs["path_prefix"] == "Day 1"


@pytest.mark.fast
def test_nothing_reported_and_no_full_scan_due_means_no_scan(das: Path) -> None:
    scan, _, _ = _cycle(FakeServer([LIB]))
    scan.assert_not_called()


@pytest.mark.fast
def test_a_full_scan_runs_when_due_and_is_remembered(das: Path) -> None:
    state = WorkerState(last_full_scan={"lib_1": 0.0})
    scan, _, state = _cycle(FakeServer([LIB]), state, now=30 * HOUR, full_scan_every=24 * HOUR)
    assert scan.call_args.kwargs["path_prefix"] is None
    assert state.last_full_scan["lib_1"] == 30 * HOUR


@pytest.mark.fast
def test_the_first_cycle_scans_every_library_in_full(das: Path) -> None:
    scan, _, _ = _cycle(FakeServer([LIB]), WorkerState())
    assert scan.call_args.kwargs["path_prefix"] is None


@pytest.mark.fast
def test_a_truncated_backlog_scans_everything(das: Path) -> None:
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]}, truncated={"lib_1"})
    scan, _, _ = _cycle(server)
    assert scan.call_args.kwargs["path_prefix"] is None
    assert server.acks == [("lib_1", [{"change_id": "chg_1", "version": 7}])]


@pytest.mark.fast
def test_sleeping_storage_is_not_scanned_but_still_enriched(home: Path) -> None:
    save_config(CLIConfig(root_map={MAC: str(home / "not-mounted")}))
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]}, summaries={"lib_1": WORK})
    scan, enrich, _ = _cycle(server, WorkerState())
    scan.assert_not_called()
    assert server.acks == []
    assert [c.args[1]["library_id"] for c in enrich.call_args_list] == ["lib_1"]


@pytest.mark.fast
def test_an_empty_mount_point_is_never_scanned(home: Path, tmp_path: Path) -> None:
    # An unmounted share is an empty folder; scanning it would mark every
    # file missing.
    mount = tmp_path / "empty-mount"
    (mount / "Footage").mkdir(parents=True)
    save_config(CLIConfig(root_map={MAC: str(mount)}))
    scan, _, _ = _cycle(FakeServer([LIB]), WorkerState())
    scan.assert_not_called()


@pytest.mark.fast
def test_a_failed_scan_keeps_the_changes(das: Path) -> None:
    server = FakeServer([LIB, {**LIB, "library_id": "lib_2", "name": "Two"}],
                        pending={"lib_1": [CHANGE], "lib_2": [{**CHANGE, "change_id": "chg_2"}]})
    scan = MagicMock(side_effect=[RuntimeError("server hiccup"), ScanStats()])
    _cycle(server, WorkerState(last_full_scan={"lib_1": 99 * HOUR, "lib_2": 99 * HOUR}), scan=scan)
    assert [lib for lib, _ in server.acks] == ["lib_2"]


@pytest.mark.fast
def test_a_scan_that_lost_its_root_keeps_the_changes(das: Path) -> None:
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _cycle(server, scan=MagicMock(return_value=ScanStats(root_unreachable=True)))
    assert server.acks == []


@pytest.mark.fast
def test_a_file_that_fails_does_not_hold_up_the_changes(das: Path) -> None:
    # Kept, every new report would widen the scan toward the whole library,
    # retrying the bad file every minute. It gets its own retry instead.
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, _, state = _cycle(server, scan=_failing("Day 1/A001.mov"))
    assert server.acks == [("lib_1", [{"change_id": "chg_1", "version": 7}])]
    assert state.retries["lib_1"].paths == {"Day 1/A001.mov"}


@pytest.mark.fast
def test_enrich_runs_only_where_there_is_work(das: Path) -> None:
    libs = [LIB, {**LIB, "library_id": "lib_2", "name": "Done"}]
    server = FakeServer(libs, summaries={"lib_1": WORK, "lib_2": {"total_assets": 5}})
    _, enrich, _ = _cycle(server, WorkerState(last_full_scan={"lib_1": 99 * HOUR, "lib_2": 99 * HOUR}))
    assert [c.args[1]["library_id"] for c in enrich.call_args_list] == ["lib_1"]
    assert enrich.call_args.kwargs["job_type"] == "all"


@pytest.mark.fast
def test_a_failed_enrich_does_not_stop_the_cycle(das: Path) -> None:
    libs = [LIB, {**LIB, "library_id": "lib_2", "name": "Two"}]
    server = FakeServer(libs, summaries={"lib_1": WORK, "lib_2": WORK})
    enrich = MagicMock(side_effect=[RuntimeError("model crashed"), None])
    _cycle(server, WorkerState(last_full_scan={"lib_1": 99 * HOUR, "lib_2": 99 * HOUR}), enrich=enrich)
    assert enrich.call_count == 2


@pytest.mark.fast
def test_an_enrich_step_that_exits_does_not_stop_the_worker(das: Path) -> None:
    # run_backfill_vision raises SystemExit(1) when vision isn't configured.
    libs = [LIB, {**LIB, "library_id": "lib_2", "name": "Two"}]
    server = FakeServer(libs, summaries={"lib_1": WORK, "lib_2": WORK})
    enrich = MagicMock(side_effect=[SystemExit(1), None])
    _cycle(server, WorkerState(last_full_scan={"lib_1": 99 * HOUR, "lib_2": 99 * HOUR}), enrich=enrich)
    assert enrich.call_count == 2


@pytest.mark.fast
def test_a_stop_signal_during_enrichment_still_stops_the_worker(das: Path) -> None:
    # main.py turns SIGTERM into SystemExit(0).
    server = FakeServer([LIB], summaries={"lib_1": WORK})
    with pytest.raises(SystemExit):
        _cycle(server, WorkerState(last_full_scan={"lib_1": 99 * HOUR}), enrich=MagicMock(side_effect=SystemExit(0)))


@pytest.mark.fast
@pytest.mark.parametrize("error", [RuntimeError("model crashed"), SystemExit(1)])
def test_a_failed_enrich_is_paced_like_any_other(das: Path, error: BaseException) -> None:
    server = FakeServer([LIB], summaries={"lib_1": WORK})
    state = WorkerState(last_full_scan={"lib_1": 100 * HOUR})
    _, _, state = _cycle(server, state, now=100 * HOUR, enrich=MagicMock(side_effect=error))
    _, again, state = _cycle(server, state, now=100 * HOUR + 60)
    _, later, _ = _cycle(server, state, now=101 * HOUR + 1)
    again.assert_not_called()
    assert later.call_count == 1


@pytest.mark.fast
def test_only_named_libraries_are_worked(das: Path) -> None:
    libs = [LIB, {**LIB, "library_id": "lib_2", "name": "Other"}]
    server = FakeServer(libs, summaries={"lib_1": WORK, "lib_2": WORK})
    _, enrich, _ = _cycle(server, WorkerState(last_full_scan={"lib_1": 99 * HOUR, "lib_2": 99 * HOUR}),
                          only=["Other"])
    assert [c.args[1]["name"] for c in enrich.call_args_list] == ["Other"]


# ---------------------------------------------------------------------------
# Process safety
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_only_one_worker_runs_per_machine(tmp_path: Path) -> None:
    first = WorkerLock(tmp_path / "worker.lock")
    second = WorkerLock(tmp_path / "worker.lock")
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


@pytest.mark.fast
def test_a_hung_probe_is_not_piled_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Each cycle probes each root; a mount that hangs must not leave a new
    # stuck thread behind every minute.
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
# CLI
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_worker_once_runs_one_cycle(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from src.client.cli import worker
    from src.client.cli.main import app

    cycles = MagicMock()
    monkeypatch.setattr(worker, "run_cycle", cycles)
    monkeypatch.setattr(worker, "LumiverbClient", MagicMock())
    result = CliRunner().invoke(app, ["worker", "--once", "--library", "Footage"])
    assert result.exit_code == 0, result.output
    assert cycles.call_count == 1
    assert cycles.call_args.kwargs["only"] == ["Footage"]
    # The lock is released afterwards.
    assert WorkerLock(home / ".cache" / "lumiverb" / "worker.lock").acquire()


@pytest.mark.fast
def test_a_second_worker_refuses_to_start(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from src.client.cli import worker
    from src.client.cli.main import app

    held = WorkerLock(home / ".cache" / "lumiverb" / "worker.lock")
    assert held.acquire()
    cycles = MagicMock()
    monkeypatch.setattr(worker, "run_cycle", cycles)
    try:
        result = CliRunner().invoke(app, ["worker", "--once"])
    finally:
        held.release()
    assert result.exit_code == 1
    assert "already running" in result.output
    cycles.assert_not_called()


@pytest.mark.fast
def test_worker_loops_until_stopped(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.client.cli import worker

    cycles = MagicMock()
    monkeypatch.setattr(worker, "run_cycle", cycles)
    monkeypatch.setattr(worker, "LumiverbClient", MagicMock())
    sleeps: list[float] = []
    worker.run_forever(poll=5, sleep=sleeps.append, stop=lambda: len(sleeps) >= 3, console=Console(quiet=True))
    assert cycles.call_count == 3
    assert sleeps == [5, 5, 5]


@pytest.mark.fast
def test_a_failed_cycle_does_not_stop_the_worker(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.client.cli import worker

    cycles = MagicMock(side_effect=[ConnectionError("API restarting"), None])
    monkeypatch.setattr(worker, "run_cycle", cycles)
    monkeypatch.setattr(worker, "LumiverbClient", MagicMock())
    sleeps: list[float] = []
    worker.run_forever(poll=1, sleep=sleeps.append, stop=lambda: len(sleeps) >= 2, console=Console(quiet=True))
    assert cycles.call_count == 2


@pytest.mark.fast
def test_an_api_not_answering_yet_is_one_line_and_tried_again_soon(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """An update restarts the API and the worker together: a refused connection
    is expected, not a failure worth a traceback, and the API is back in seconds."""
    import logging

    import httpx

    from src.client.cli import worker

    cycles = MagicMock(side_effect=[httpx.ConnectError("Connection refused"), None])
    monkeypatch.setattr(worker, "run_cycle", cycles)
    monkeypatch.setattr(worker, "LumiverbClient", MagicMock())
    sleeps: list[float] = []
    with caplog.at_level(logging.INFO, logger="src.client.cli.worker"):
        worker.run_forever(poll=60, sleep=sleeps.append, stop=lambda: len(sleeps) >= 2, console=Console(quiet=True))
    assert cycles.call_count == 2
    assert sleeps == [10, 60]
    [record] = [r for r in caplog.records if "answering" in r.getMessage()]
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert "trying again in 10s" in record.getMessage()


@pytest.mark.fast
def test_worker_start_clears_proxies_cut_off_by_a_kill(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A SIGKILL mid-render or mid-download leaves these; eviction only
    # counts finished proxies, so nothing else would remove them.
    from src.client.cli import worker

    cache = home / ".cache" / "lumiverb" / "analysis"
    cache.mkdir(parents=True)
    for name in ("ast_a.mp4", "ast_b.rendering", "ast_c.rendering.part", "ast_d.mp4.part"):
        (cache / name).write_bytes(b"x")
    monkeypatch.setattr(worker, "run_cycle", MagicMock())
    monkeypatch.setattr(worker, "LumiverbClient", MagicMock())
    worker.run_forever(once=True, console=Console(quiet=True))
    assert sorted(p.name for p in cache.iterdir()) == ["ast_a.mp4"]


@pytest.mark.fast
def test_full_scan_times_survive_a_restart(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Otherwise every deploy scans every library in full.
    from src.client.cli import worker

    def scanned(client, *, state, **kw):
        state.last_full_scan["lib_1"] = 123.0

    seen: list[dict] = []
    monkeypatch.setattr(worker, "LumiverbClient", MagicMock())
    monkeypatch.setattr(worker, "run_cycle", scanned)
    worker.run_forever(once=True, console=Console(quiet=True))
    monkeypatch.setattr(worker, "run_cycle", lambda client, *, state, **kw: seen.append(dict(state.last_full_scan)))
    worker.run_forever(once=True, console=Console(quiet=True))
    assert seen == [{"lib_1": 123.0}]


@pytest.mark.fast
def test_a_full_scan_time_survives_a_stop_mid_cycle(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A stop (SIGTERM: systemctl stop, a deploy) during the enrichment that
    # follows a full scan mustn't mean a second full scan on the next start.
    from src.client.cli import worker

    def scanned_then_stopped(client, *, state, **kw):
        state.last_full_scan["lib_1"] = 123.0
        raise SystemExit(0)

    seen: list[dict] = []
    monkeypatch.setattr(worker, "LumiverbClient", MagicMock())
    monkeypatch.setattr(worker, "run_cycle", scanned_then_stopped)
    with pytest.raises(SystemExit):
        worker.run_forever(once=True, console=Console(quiet=True))
    monkeypatch.setattr(worker, "run_cycle", lambda client, *, state, **kw: seen.append(dict(state.last_full_scan)))
    worker.run_forever(once=True, console=Console(quiet=True))
    assert seen == [{"lib_1": 123.0}]


@pytest.mark.fast
def test_files_waiting_for_a_retry_survive_a_restart(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Otherwise they'd wait for the next full scan, up to a day, and the
    # changes held for them would be rescanned at once.
    from src.client.cli import worker
    from src.client.cli.worker import Retry

    waiting = Retry({"Day 1/A001.mov", "Locked"}, 600.0, 1234.5, {"chg_1": 7})

    def failed(client, *, state, **kw):
        state.retries["lib_1"] = waiting

    seen: list[dict] = []
    monkeypatch.setattr(worker, "LumiverbClient", MagicMock())
    monkeypatch.setattr(worker, "run_cycle", failed)
    worker.run_forever(once=True, console=Console(quiet=True))
    monkeypatch.setattr(worker, "run_cycle", lambda client, *, state, **kw: seen.append(dict(state.retries)))
    worker.run_forever(once=True, console=Console(quiet=True))
    assert seen == [{"lib_1": waiting}]


@pytest.mark.fast
@pytest.mark.parametrize("content", ["not json", '["a list"]', '{"last_full_scan": {"lib_1": "soon"}}',
                                     '{"last_full_scan": {}, "retries": {"lib_1": {"paths": 3}}}'])
def test_an_unreadable_state_file_means_full_scans(home: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    from src.client.cli import worker

    path = home / ".cache" / "lumiverb" / "worker-state.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    seen: list[dict] = []
    monkeypatch.setattr(worker, "LumiverbClient", MagicMock())
    monkeypatch.setattr(worker, "run_cycle", lambda client, *, state, **kw: seen.append(dict(state.last_full_scan)))
    worker.run_forever(once=True, console=Console(quiet=True))
    assert seen == [{}]


@pytest.mark.fast
def test_clearing_leftovers_without_a_cache_is_fine(tmp_path: Path) -> None:
    from src.client.proxy.analysis_cache import clear_leftovers

    assert clear_leftovers(tmp_path / "never-made") == 0


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


# ---------------------------------------------------------------------------
# Scanning first, then enrichment for a limited time: a first ingest's
# enrichment can run for days, and change reports mustn't wait on it.
# ---------------------------------------------------------------------------

TWO = [LIB, {**LIB, "library_id": "lib_2", "name": "Two"}]
BOTH_SCANNED = {"lib_1": 99 * HOUR, "lib_2": 99 * HOUR}


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _enriched(enrich: MagicMock) -> list[str]:
    return [c.args[1]["library_id"] for c in enrich.call_args_list]


@pytest.mark.fast
def test_every_library_is_scanned_before_any_is_enriched(das: Path) -> None:
    server = FakeServer(TWO, pending={"lib_1": [CHANGE], "lib_2": [{**CHANGE, "change_id": "chg_2"}]},
                        summaries={"lib_1": WORK, "lib_2": WORK})
    order: list[tuple[str, str]] = []
    scan = MagicMock(side_effect=lambda client, lib, **kw: order.append(("scan", lib["library_id"])) or ScanStats())
    enrich = MagicMock(side_effect=lambda client, lib, **kw: order.append(("enrich", lib["library_id"])))
    _cycle(server, WorkerState(last_full_scan=dict(BOTH_SCANNED)), scan=scan, enrich=enrich)
    assert order == [("scan", "lib_1"), ("scan", "lib_2"), ("enrich", "lib_1"), ("enrich", "lib_2")]


@pytest.mark.fast
def test_enrichment_stops_when_its_time_is_up(das: Path) -> None:
    server = FakeServer(TWO, summaries={"lib_1": WORK, "lib_2": WORK})
    clock = Clock()
    told: list[bool] = []

    def enrich(client, lib, *, should_stop, **kw):
        told.append(should_stop())
        clock.t += 16 * MIN
        told.append(should_stop())

    _, mock, _ = _cycle(server, WorkerState(last_full_scan=dict(BOTH_SCANNED)),
                        enrich=MagicMock(side_effect=enrich), clock=clock)
    assert _enriched(mock) == ["lib_1"]
    assert told == [False, True]


@pytest.mark.fast
def test_a_library_cut_short_goes_on_next_cycle_after_the_others(das: Path) -> None:
    server = FakeServer(TWO, summaries={"lib_1": WORK, "lib_2": WORK})
    clock = Clock()

    def enrich(client, lib, *, on_take, **kw):
        on_take("render", f"ast_{clock.t}")  # a long render that failed
        clock.t += 16 * MIN

    state = WorkerState(last_full_scan=dict(BOTH_SCANNED))
    runs = []
    for minute in range(3):
        _, mock, state = _cycle(server, state, now=100 * HOUR + minute * MIN,
                                enrich=MagicMock(side_effect=enrich), clock=clock)
        runs.append(_enriched(mock))
    # lib_1's counts didn't change, but it wasn't finished, so it isn't paced.
    assert runs == [["lib_1"], ["lib_2"], ["lib_1"]]


class FakeRenders:
    """run_repair's render step over `missing`: "bad" takes 16 minutes and fails, the rest a minute each."""

    def __init__(self, server: FakeServer, clock: Clock, missing: list[str]) -> None:
        self.server, self.clock, self.missing = server, clock, missing
        self.runs: list[list[str]] = []
        self._count()

    def _count(self) -> None:
        self.server.summaries["lib_1"] = {"total_assets": 3, "missing_analysis_proxy": len(self.missing)}

    def __call__(self, client, lib, *, should_stop, skip_items, on_take, **kw) -> None:
        run: list[str] = []
        self.runs.append(run)
        for asset_id in list(self.missing):
            if ("render", asset_id) in skip_items:
                continue
            if should_stop():
                return
            on_take("render", asset_id)
            run.append(asset_id)
            if asset_id == "bad":
                self.clock.t += 16 * MIN
            else:
                self.clock.t += MIN
                self.missing.remove(asset_id)
                self._count()


@pytest.mark.fast
def test_an_item_that_keeps_failing_does_not_hold_up_the_rest(das: Path) -> None:
    server = FakeServer([LIB])
    clock = Clock()
    renders = FakeRenders(server, clock, ["bad", "a", "b"])
    state = WorkerState(last_full_scan={"lib_1": 100 * HOUR})
    for minute in (0, 1, 2, 61):
        _cycle(server, state, now=100 * HOUR + minute * MIN, enrich=renders, clock=clock)
    # The next cycle goes on past it; it's tried again after an hour.
    assert renders.runs == [["bad"], ["a", "b"], ["bad"]]


@pytest.mark.fast
def test_a_run_cut_short_without_getting_anywhere_is_paced(das: Path) -> None:
    # It took nothing new and changed nothing: running it again every
    # cycle would only use up every cycle's time.
    server = FakeServer([LIB], summaries={"lib_1": WORK})
    clock = Clock()

    def enrich(client, lib, **kw):
        clock.t += 16 * MIN

    state = WorkerState(last_full_scan={"lib_1": 100 * HOUR})
    calls = []
    for minute in (0, 1, 61):
        _, mock, state = _cycle(server, state, now=100 * HOUR + minute * MIN,
                                enrich=MagicMock(side_effect=enrich), clock=clock)
        calls.append(mock.call_count)
    assert calls == [1, 0, 1]


@pytest.mark.fast
def test_a_library_whose_time_ran_out_before_it_got_going_goes_first_next_cycle(das: Path) -> None:
    # lib_1 used most of the time; lib_2 spent the rest loading a model and
    # took nothing. That's not stuck: it isn't paced, and it isn't sent to
    # the back of the queue either.
    server = FakeServer(TWO, summaries={"lib_1": WORK, "lib_2": WORK})
    clock = Clock()
    took: list[str] = []

    def enrich(client, lib, *, should_stop, on_take, **kw):
        if lib["library_id"] == "lib_1":
            on_take("render", f"ast_{clock.t}")
            clock.t += 13 * MIN
            return
        clock.t += 3 * MIN  # loading Whisper, listing what's missing
        if not should_stop():
            on_take("transcribe", "ast_x")
            took.append(lib["library_id"])

    state = WorkerState(last_full_scan=dict(BOTH_SCANNED))
    _, first, state = _cycle(server, state, enrich=MagicMock(side_effect=enrich), clock=clock)
    assert _enriched(first) == ["lib_1", "lib_2"] and took == []
    server.summaries["lib_1"] = {**WORK, "missing_transcription": 2}  # a new clip: lib_1 has work too
    _, second, state = _cycle(server, state, now=100 * HOUR + MIN, enrich=MagicMock(side_effect=enrich), clock=clock)
    assert _enriched(second) == ["lib_2", "lib_1"]
    assert took == ["lib_2"]


@pytest.mark.fast
def test_a_library_is_not_started_with_almost_no_time_left(das: Path) -> None:
    # It would run out of time getting going, and be paced as if stuck.
    server = FakeServer(TWO, summaries={"lib_1": WORK, "lib_2": WORK})
    clock = Clock()

    def enrich(client, lib, **kw):
        clock.t += 14.5 * MIN
        return None

    _, mock, state = _cycle(server, WorkerState(last_full_scan=dict(BOTH_SCANNED)),
                            enrich=MagicMock(side_effect=enrich), clock=clock)
    assert _enriched(mock) == ["lib_1"]
    assert "lib_2" not in state.last_enrich


# ---------------------------------------------------------------------------
# Pacing enrichment
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_enrich_is_not_repeated_while_nothing_changes(das: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A clip that fails every time shouldn't be retried every minute.
    monkeypatch.setattr("src.client.cli.worker._vision_ready", lambda client: True)
    server = FakeServer([LIB], summaries={"lib_1": WORK})
    state = WorkerState(last_full_scan={"lib_1": 100 * HOUR})
    _, enrich, state = _cycle(server, state, now=100 * HOUR)
    _, enrich2, state = _cycle(server, state, now=100 * HOUR + 60)
    assert enrich.call_count == 1
    enrich2.assert_not_called()


@pytest.mark.fast
def test_enrich_runs_again_when_the_counts_change(das: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.worker._vision_ready", lambda client: True)
    server = FakeServer([LIB], summaries={"lib_1": WORK})
    state = WorkerState(last_full_scan={"lib_1": 100 * HOUR})
    _, _, state = _cycle(server, state, now=100 * HOUR)
    server.summaries["lib_1"] = {**WORK, "missing_transcription": 2}  # a new clip arrived
    _, enrich, _ = _cycle(server, state, now=100 * HOUR + 60)
    assert enrich.call_count == 1


@pytest.mark.fast
def test_failures_are_retried_hourly(das: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.worker._vision_ready", lambda client: True)
    server = FakeServer([LIB], summaries={"lib_1": WORK})
    state = WorkerState(last_full_scan={"lib_1": 100 * HOUR})
    _, _, state = _cycle(server, state, now=100 * HOUR)
    _, enrich, _ = _cycle(server, state, now=101 * HOUR + 1)
    assert enrich.call_count == 1


@pytest.mark.fast
def test_enrich_runs_when_storage_wakes(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Rendering waited for the storage; it can go as soon as it's back.
    monkeypatch.setattr("src.client.cli.worker._vision_ready", lambda client: True)
    mount = tmp_path / "mnt"
    save_config(CLIConfig(root_map={MAC: str(mount)}))
    server = FakeServer([LIB], summaries={"lib_1": {"total_assets": 1, "missing_analysis_proxy": 1}})
    state = WorkerState(last_full_scan={"lib_1": 100 * HOUR})
    _, asleep, state = _cycle(server, state, now=100 * HOUR)
    (mount / "Footage" / "Day 1").mkdir(parents=True)
    (mount / "Footage" / "Day 1" / "A001.mov").write_bytes(b"x")
    _, awake, _ = _cycle(server, state, now=100 * HOUR + 60)
    assert asleep.call_count == 1 and awake.call_count == 1


@pytest.mark.fast
def test_without_vision_ai_its_steps_are_skipped(das: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.worker._vision_ready", lambda client: False)
    server = FakeServer([LIB], summaries={"lib_1": {"total_assets": 3, "missing_scene_vision": 3,
                                                    "missing_vision": 2, "missing_ocr": 1}})
    _, enrich, _ = _cycle(server, WorkerState(last_full_scan={"lib_1": 100 * HOUR}))
    enrich.assert_not_called()

    server.summaries["lib_1"]["missing_transcription"] = 1
    _, enrich, _ = _cycle(server, WorkerState(last_full_scan={"lib_1": 100 * HOUR}))
    assert enrich.call_args.kwargs["skip_types"] == {"vision", "ocr", "scene-vision"}


@pytest.mark.fast
def test_without_a_machine_for_transcripts_transcription_is_skipped(das: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Transcripts off, or no machine doing them online: clips waiting for one
    don't count as work, so the library isn't enriched for them every cycle."""
    monkeypatch.setattr("src.client.cli.worker._transcripts_ready", lambda client: False)
    server = FakeServer([LIB], summaries={"lib_1": {"total_assets": 2, "missing_transcription": 2}})
    _, enrich, _ = _cycle(server, WorkerState(last_full_scan={"lib_1": 100 * HOUR}))
    enrich.assert_not_called()

    server.summaries["lib_1"]["missing_vision"] = 1
    _, enrich, _ = _cycle(server, WorkerState(last_full_scan={"lib_1": 100 * HOUR}))
    assert enrich.call_args.kwargs["skip_types"] == {"transcribe"}


@pytest.mark.fast
def test_files_still_being_written_keep_the_changes(das: Path) -> None:
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _cycle(server, scan=MagicMock(return_value=ScanStats(settling=1, settling_paths=["Day 1/A001.mov"])))
    assert server.acks == []


@pytest.mark.fast
def test_only_changes_covering_a_file_still_being_written_wait(das: Path) -> None:
    changes = [CHANGE, {**CHANGE, "change_id": "chg_2", "rel_path": "Day 1/A002.mov"},
               {**CHANGE, "change_id": "chg_3", "rel_path": "Day 1"}]
    server = FakeServer([LIB], pending={"lib_1": changes})
    _cycle(server, scan=MagicMock(return_value=ScanStats(settling=1, settling_paths=["Day 1/A001.mov"])))
    assert server.acks == [("lib_1", [{"change_id": "chg_2", "version": 7}])]


# ---------------------------------------------------------------------------
# A file that always fails is retried with back-off, not every cycle
# ---------------------------------------------------------------------------

MIN = 60.0


def _failing(*paths: str) -> MagicMock:
    return MagicMock(return_value=ScanStats(failed=len(paths), failed_paths=list(paths)))


@pytest.mark.fast
def test_a_failed_file_is_tried_again_with_back_off(das: Path) -> None:
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, _, state = _cycle(server, now=t, scan=_failing("Day 1/A001.mov"))
    server.pending = {}
    tries = []
    for minute in range(1, 40):
        scan, _, state = _cycle(server, state, now=t + minute * MIN, scan=_failing("Day 1/A001.mov"))
        if scan.called:
            assert scan.call_args.kwargs["path_prefix"] == "Day 1"
            tries.append(minute)
    assert tries == [5, 15, 35]


@pytest.mark.fast
def test_back_off_stops_growing_at_a_day(das: Path) -> None:
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    state = WorkerState(last_full_scan={"lib_1": t})
    _cycle(server, state, now=t, scan=_failing("Day 1/A001.mov"), full_scan_every=1e9)
    server.pending = {}
    for _ in range(12):
        _cycle(server, state, now=state.retries["lib_1"].due, scan=_failing("Day 1/A001.mov"), full_scan_every=1e9)
    assert state.retries["lib_1"].delay == 24 * HOUR


@pytest.mark.fast
def test_a_clean_scan_of_the_folder_ends_the_retries(das: Path) -> None:
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, _, state = _cycle(server, now=t, scan=_failing("Day 1/A001.mov"))
    # The Mac reports the file again (fixed, say) before the retry is due.
    _, _, state = _cycle(server, state, now=t + MIN)
    assert state.retries == {}
    server.pending = {}
    scan, _, _ = _cycle(server, state, now=t + HOUR)
    scan.assert_not_called()


@pytest.mark.fast
def test_a_retry_not_yet_due_does_not_widen_other_scans(das: Path) -> None:
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, _, state = _cycle(server, now=t, scan=_failing("Day 1/A001.mov"))
    server.pending = {"lib_1": [{**CHANGE, "change_id": "chg_2", "rel_path": "Day 2/B001.mov"}]}
    scan, _, state = _cycle(server, state, now=t + MIN)
    assert scan.call_args.kwargs["path_prefix"] == "Day 2"
    assert state.retries["lib_1"].paths == {"Day 1/A001.mov"}


# ---------------------------------------------------------------------------
# A folder that couldn't be listed: its changes wait, with back-off
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_a_scan_that_could_not_list_a_folder_keeps_the_changes(das: Path) -> None:
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _cycle(server, scan=MagicMock(return_value=ScanStats(unlisted=["Day 1"])))
    assert server.acks == []


@pytest.mark.fast
def test_changes_under_a_folder_that_could_not_be_listed_wait_for_the_retry(das: Path) -> None:
    # A folder that stays unreadable (chmod 000) mustn't be rescanned every minute.
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, _, state = _cycle(server, now=t, scan=MagicMock(return_value=ScanStats(unlisted=["Day 1"])))
    scan, _, state = _cycle(server, state, now=t + MIN)
    scan.assert_not_called()
    scan, _, state = _cycle(server, state, now=t + 5 * MIN)
    assert scan.call_args.kwargs["path_prefix"] == "Day 1"
    assert server.acks == [("lib_1", [{"change_id": "chg_1", "version": 7}])]
    assert state.retries == {}


@pytest.mark.fast
def test_a_change_reported_again_does_not_wait_for_the_retry(das: Path) -> None:
    t = 100 * HOUR
    server = FakeServer([LIB], pending={"lib_1": [CHANGE]})
    _, _, state = _cycle(server, now=t, scan=MagicMock(return_value=ScanStats(unlisted=["Day 1"])))
    server.pending = {"lib_1": [{**CHANGE, "version": 8}]}
    scan, _, _ = _cycle(server, state, now=t + MIN)
    assert scan.call_args.kwargs["path_prefix"] == "Day 1"
    assert server.acks == [("lib_1", [{"change_id": "chg_1", "version": 8}])]
