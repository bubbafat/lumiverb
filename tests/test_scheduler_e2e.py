# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The scheduler end to end, against a real database and API (ADR-016 phase 4).

A clip due for a description is found in the account's database, handed
to the vision pool, described, and saved through the API with current
lineage: after that it's no longer due. The scheduler reaches the API with
a key it makes itself at start, revoking the one before.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.server.api.main import app
from src.server.scheduler.kinds import KINDS
from src.server.scheduler.queue import candidates
from src.server.scheduler.scans import ScanState
from src.server.scheduler.service import Scheduler, scheduler_key
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha
from tests.test_reconciler import _library

pytestmark = pytest.mark.slow


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", lambda: h)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    return h


def _client(key: str):
    """The scheduler's API client, talking to the app in-process."""
    from src.client.cli.client import LumiverbClient

    client = LumiverbClient(base_url="http://testserver", token=key)
    client._client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return client


def test_the_scheduler_makes_its_own_key_and_revokes_the_last(env) -> None:
    tenant_id = env[4]
    first = scheduler_key(tenant_id)
    assert _client(first).get("/v1/libraries").status_code == 200
    second = scheduler_key(tenant_id)
    probe = TestClient(app)
    assert probe.get("/v1/libraries", headers={"Authorization": f"Bearer {first}"}).status_code == 401
    assert probe.get("/v1/libraries", headers={"Authorization": f"Bearer {second}"}).status_code == 200


class FakePool:
    """A job's machines, as far as sharing the GPU goes."""

    def __init__(self) -> None:
        self.gpu_hold = 0

    def set_gpu_hold(self, hold: int) -> None:
        self.gpu_hold = hold


class FakeVision:
    """The account's vision machines: one, offering the model."""

    model = "qwen3-vl:8b-instruct"
    down = False
    error = None

    def __init__(self) -> None:
        self.pool = FakePool()

    def check(self) -> bool:
        return True

    def capacity(self) -> int:
        return 1

    def charges(self, error) -> bool:
        return True

    def provider(self, settings=None, ocr_settings=None):
        provider = MagicMock()
        provider.describe.return_value = {"description": "a red square", "tags": ["red", "square"]}
        return provider


class NoMachines:
    model = ""
    down = True
    error = "no machine"

    def __init__(self) -> None:
        self.pool = FakePool()

    def check(self) -> bool:
        return False


def test_a_description_goes_from_the_queue_to_the_database(env, home: Path) -> None:
    from src.server.scheduler.account import Account

    lib = _library(env, "Scheduler end to end")
    photo = _ingest_with(lib, "red.jpg", _sha(), None)
    acct = Account(env[4], _client(scheduler_key(env[4])), ScanState())
    acct.guards = {"vision": FakeVision(), "transcripts": NoMachines()}
    acct.refresh(force=True)
    assert acct.settings_ready  # the server's settings were read
    # Only the vision pool has slots: nothing else runs here.
    s = Scheduler(lambda: {env[4]: acct}, capacity={"scan": 0})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        s.tick()
        s.collect(timeout=0.5)
        with _db(env) as session:
            if photo not in [i["asset_id"] for i in candidates(session, KINDS["vision"], [lib[2]])]:
                break
    s.stop()
    client, headers, *_ = lib
    r = client.get(f"/v1/assets/{photo}", headers=headers)
    assert r.status_code == 200, r.text
    assert "a red square" in r.text
    with _db(env) as session:
        assert photo not in [i["asset_id"] for i in candidates(session, KINDS["vision"], [lib[2]])]


def test_a_job_reads_its_clips_files_from_the_store_by_the_keys_its_rows_carry(env) -> None:
    """Beside the API (its database and disk): every kind's due clips carry
    their stored preview's and analysis proxy's keys, and a job's inputs
    read them from the Store: no lookup of its own, no cache asked, no copy."""
    from src.server.scheduler.inputs import Inputs
    from src.server.storage.store import get_store
    from tests.test_analysis_proxy_api import _ingest, _upload
    from tests.videos import playable

    video = _ingest(env, "stored/a.mov")
    up = _upload(env, video, playable())
    assert up.status_code == 200, up.text
    photo = _ingest(env, "stored/c.jpg", media_type="image")
    rows: dict[str, dict] = {}
    with _db(env) as session:
        for kind in (k for k in KINDS.values() if not k.redo):
            for row in candidates(session, kind, [env[2]], limit=10_000):
                assert {"proxy_key", "analysis_proxy_key"} <= set(row), kind.name
                rows.setdefault(row["asset_id"], row)
    assert {video, photo} <= set(rows), "each is due for something"
    acct = MagicMock()
    inputs = Inputs(acct, get_store())
    with inputs.analysis_proxy(rows[video]) as path:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == up.json()["sha256"]  # what was kept
    assert inputs.preview_bytes(rows[photo])[8:12] == b"WEBP"
    acct.analysis_cache.held.assert_not_called()
    acct.proxy_cache.assert_not_called()
