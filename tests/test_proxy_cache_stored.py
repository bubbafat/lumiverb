"""Where the API keeps the previews on this machine, a job reads each clip's
stored preview from the Store (src/server/scheduler/inputs.py,
tests/test_inputs.py), and the copies an earlier scheduler kept in its
preview cache go at its start (forget_preview_copies): 2 GB of them on
Robert's Mac. Never a cache a CLI or the Mac app shares."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.fast


@pytest.fixture(autouse=True)
def _cache_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _service(monkeypatch, data_dir: Path, *, here: bool = True):
    from src.server.scheduler import service

    monkeypatch.setattr(service, "stores_here", lambda: here)
    monkeypatch.setattr("src.server.config.get_settings", lambda: SimpleNamespace(data_dir=str(data_dir)))
    return service


def _copies(folder: Path, n: int) -> None:
    folder.mkdir(parents=True)
    for i in range(n):
        (folder / f"ast_{i}").write_bytes(b"x" * 1000)
        (folder / f"ast_{i}.sha").write_text("sha")


def test_the_copies_an_earlier_scheduler_kept_go_at_its_start(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_CACHE_HOME", str(data / "cache"))  # where run puts the scheduler's
    _copies(data / "cache" / "lumiverb" / "proxies", 3)
    service = _service(monkeypatch, data)
    assert service.forget_preview_copies() == (6, 3009)
    assert not (data / "cache" / "lumiverb" / "proxies").exists()
    assert service.forget_preview_copies() is None  # nothing left to do


def test_a_cache_outside_the_servers_data_or_with_previews_elsewhere_is_left_alone(tmp_path, monkeypatch):
    data = tmp_path / "data"
    shared = tmp_path / "home-cache"  # the CLI's and the Mac app's: never the scheduler's to clear
    monkeypatch.setenv("XDG_CACHE_HOME", str(shared))
    _copies(shared / "lumiverb" / "proxies", 1)
    assert _service(monkeypatch, data).forget_preview_copies() is None
    assert (shared / "lumiverb" / "proxies" / "ast_0").exists()
    monkeypatch.setenv("XDG_CACHE_HOME", str(data / "cache"))
    _copies(data / "cache" / "lumiverb" / "proxies", 1)
    assert _service(monkeypatch, data, here=False).forget_preview_copies() is None  # previews kept elsewhere
    assert (data / "cache" / "lumiverb" / "proxies" / "ast_0").exists()
