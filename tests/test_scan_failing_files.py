# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A file that keeps failing waits for its retry (Robert, Oct 13).

With a full scan of each library every 15 minutes, a broken file (a video
that won't decode) was read again and failed again, with a traceback in the
log, by every one of them on top of its own back-off: 96 tries a day. Now a
scan leaves a failing file to its retry: found, so never taken for missing,
but neither read nor uploaded, unless it changed since it failed (replaced,
say), when it's tried at once.

The scheduler's scan pass with the real run_scan, as the scheduler runs
them, over a real folder against the real API, on the scheduler's clock.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from src.processing import scan as scan_module
from src.server.scheduler.scans import RETRY_FIRST_SEC, ScanState, scan_pass
from tests.producer_fakes import scheduler_client
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_scan_outcomes import HOUR, Lib, _ago, _image, _in_sight, _missing, _set_mtime

pytestmark = pytest.mark.slow

MIN = 60.0
T = 1000 * HOUR  # the scheduler's clock


@pytest.fixture(scope="module")
def api(env):
    return scheduler_client(env[4])


@pytest.fixture(autouse=True)
def _cache_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def lib(env, tmp_path: Path) -> Lib:
    client, headers, *_ = env
    root = tmp_path / "library"
    root.mkdir()
    r = client.post("/v1/libraries", json={"name": f"Failing {uuid.uuid4().hex[:10]}", "root_path": str(root)},
                    headers=headers)
    assert r.status_code == 200, r.text
    return Lib(root, r.json())


@pytest.fixture
def read(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each file a scan reads: hashed, or its preview made."""
    names: list[str] = []
    real_sha, real_proxy = scan_module.content_hash, scan_module._generate_proxy_bytes

    def sha(path):
        names.append(Path(path).name)
        return real_sha(path)

    def proxy(path):
        names.append(Path(path).name)
        return real_proxy(path)

    monkeypatch.setattr(scan_module, "content_hash", sha)
    monkeypatch.setattr(scan_module, "_generate_proxy_bytes", proxy)
    return names


def _broken(lib: Lib, rel: str, n: int) -> None:
    """A file no preview can be made of; n bytes long, written an hour ago."""
    path = lib.root / rel
    path.write_bytes(b"x" * n)
    _set_mtime(path, _ago(HOUR))


def _pass(api, lib: Lib, state: ScanState, read: list[str], minute: float) -> set[str]:
    """One look of the scheduler's at the library, a full scan due every 2
    minutes; the files it read."""
    read.clear()
    scan_pass(api, [lib.library], state, now=T + minute * MIN, full_scan_every=2 * MIN)
    return set(read)


def test_a_failing_file_waits_for_its_retry_unless_it_changed_and_is_never_taken_for_missing(env, api, lib, read):
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    state = ScanState()
    assert _pass(api, lib, state, read, 0) == {"a.png", "b.png"}  # the first full scan
    a = _in_sight(env, lib)["a.png"]["asset_id"]

    # a.png's content is replaced by something no preview can be made of, and a broken new file arrives.
    _broken(lib, "a.png", 500)
    os.utime(lib.root / "a.png", (_ago(2 * HOUR), _ago(2 * HOUR)))
    _broken(lib, "broken.jpg", 700)
    assert _pass(api, lib, state, read, 2) == {"a.png", "broken.jpg"}  # a full scan: both fail
    retry = state.retries[lib.id]
    assert retry.paths == {"a.png", "broken.jpg"} and retry.due == T + 2 * MIN + RETRY_FIRST_SEC

    # Full scans before the retry is due leave them alone: neither read nor
    # uploaded, and a.png's clip stays in sight (its file is there).
    assert _pass(api, lib, state, read, 4) == set()
    assert state.retries[lib.id].due == retry.due and state.retries[lib.id].paths == retry.paths
    assert _in_sight(env, lib)["a.png"]["asset_id"] == a and _missing(env, lib) == {}

    # broken.jpg replaced (its size changes): the next full scan tries it at once, a.png still waits.
    _broken(lib, "broken.jpg", 900)
    assert _pass(api, lib, state, read, 6) == {"broken.jpg"}
    due = state.retries[lib.id].due  # it failed again: the retry is put off, twice as long
    assert due == T + 6 * MIN + 2 * RETRY_FIRST_SEC
    assert _pass(api, lib, state, read, 8) == set()

    # The retry, when due, tries both.
    assert _pass(api, lib, state, read, (due - T) / MIN) == {"a.png", "broken.jpg"}
    assert state.retries[lib.id].paths == {"a.png", "broken.jpg"}
    assert _in_sight(env, lib)["a.png"]["asset_id"] == a and _missing(env, lib) == {}
    assert "broken.jpg" not in _in_sight(env, lib)

    # Mended (a.png's old content back, broken.jpg a picture): the next scan
    # takes them in and the retry ends. a.png's clip is still its own (new
    # content would have been a new clip: test_scan_outcomes.py).
    _image(lib, "a.png", 1)
    _image(lib, "broken.jpg", 4)
    later = (state.retries[lib.id].due - T) / MIN
    assert _pass(api, lib, state, read, later) == {"a.png", "broken.jpg"}
    assert lib.id not in state.retries
    clips = _in_sight(env, lib)
    assert clips["a.png"]["asset_id"] == a and "broken.jpg" in clips and _missing(env, lib) == {}
