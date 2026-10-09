"""What one account's jobs run with (ADR-016 phase 4).

Each library's storage is looked at by the scan pass only, and jobs go by
that look; a job that finds the storage gone puts its storage work off
until the next look. Models unused for a while are let go of, so the GPU
is free for the rest.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.server.scheduler import account as account_mod
from src.server.scheduler.scans import ScanState

pytestmark = pytest.mark.fast


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def acct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    clock = Clock()
    a = account_mod.Account("t1", MagicMock(), ScanState(), clock=clock)
    a.clock = clock
    return a


def test_storage_work_goes_by_the_scan_passs_look(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}, {"library_id": "lib_2", "name": "B"}])
    assert acct.library_ids(storage=True) == []  # not looked at yet
    acct.set_reachable({"lib_1": tmp_path, "lib_2": None})
    assert acct.library_ids(storage=True) == ["lib_1"]
    assert acct.library_ids(storage=False) == ["lib_1", "lib_2"]
    assert acct.root("lib_1") == tmp_path and acct.root("lib_2") is None
    acct.unreachable("lib_1")  # a job found it gone
    assert acct.library_ids(storage=True) == [] and acct.reachable == {"lib_1": False, "lib_2": False}


def test_a_proxy_cache_follows_its_librarys_storage(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}])
    first = acct.proxy_cache("lib_1")
    assert first._root_path is None  # not reachable at first: proxies come from the server
    acct.set_reachable({"lib_1": tmp_path})
    assert acct.proxy_cache("lib_1")._root_path == tmp_path  # made from the originals now


def test_nothing_is_ready_until_the_servers_settings_were_read(acct) -> None:
    acct.vision.check = MagicMock(return_value=False)
    acct.transcripts.check = MagicMock(return_value=False)
    acct.client.get.side_effect = ConnectionError("the API isn't answering yet")
    acct.refresh()
    assert acct.settings_ready is False
    acct.client.get.side_effect = None
    acct.client.get.return_value.json.return_value = {"producers": [], "libraries": []}
    acct.clock.now += account_mod.RETRY_SEC  # tried again sooner than the usual minute
    acct.refresh()
    assert acct.settings_ready is True


def test_idle_models_are_let_go_of(acct) -> None:
    acct.vision.check = MagicMock(return_value=True)
    acct.transcripts.check = MagicMock(return_value=True)
    acct.client.get.return_value.json.return_value = {"producers": []}
    acct._clip = (("ViT-B-32", "openai"), MagicMock())
    acct._used["clip"] = acct.clock.now
    faces = MagicMock(idle=True)
    acct._faces = faces
    acct._used["faces"] = acct.clock.now
    acct.clock.now += account_mod.IDLE_SEC - 1
    acct.refresh()
    assert acct._clip is not None and not faces.close.called
    acct.clock.now += 2
    acct.refresh()
    assert acct._clip is None and faces.close.called


def test_a_face_process_in_the_middle_of_a_batch_isnt_let_go_of(acct) -> None:
    acct.vision.check = MagicMock(return_value=True)
    acct.transcripts.check = MagicMock(return_value=True)
    acct.client.get.return_value.json.return_value = {"producers": []}
    faces = MagicMock(idle=False)
    acct._faces = faces
    acct._used["faces"] = acct.clock.now
    acct.clock.now += account_mod.IDLE_SEC + 1
    acct.refresh()
    assert not faces.close.called


def test_closing_tells_jobs_to_save_nothing_more(acct) -> None:
    acct.close()
    assert acct.stopping.is_set()
