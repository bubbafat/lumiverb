"""Where Lumiverb's local caches live: ~/.cache/lumiverb, or $XDG_CACHE_HOME/lumiverb.

The brain's worker sets XDG_CACHE_HOME to its data disk, so proxy caches
don't fill the root disk during the first ingest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.client.cli.worker import WorkerLock
from src.client.proxy.analysis_cache import AnalysisProxyCache
from src.client.proxy.proxy_cache import ProxyCache

pytestmark = pytest.mark.fast


def test_caches_follow_xdg_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert ProxyCache().path == tmp_path / "xdg" / "lumiverb" / "proxies"
    assert AnalysisProxyCache(object(), max_bytes=1).path_for("a").parent == tmp_path / "xdg" / "lumiverb" / "analysis"
    assert WorkerLock()._path == tmp_path / "xdg" / "lumiverb" / "worker.lock"


def test_caches_default_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ProxyCache().path == tmp_path / ".cache" / "lumiverb" / "proxies"
    assert AnalysisProxyCache(object(), max_bytes=1).path_for("a").parent == tmp_path / ".cache" / "lumiverb" / "analysis"


def test_caches_follow_the_cli_config_without_xdg_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `sudo -u lumiverb -H lumiverb worker --once` gets no XDG_CACHE_HOME; it
    # must still find the service's lock, so two workers never run at once.
    from src.client.cli.config import CLIConfig, save_config

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    save_config(CLIConfig(cache_home=str(tmp_path / "data" / "cache")))
    assert WorkerLock()._path == tmp_path / "data" / "cache" / "lumiverb" / "worker.lock"
    assert ProxyCache().path == tmp_path / "data" / "cache" / "lumiverb" / "proxies"


def test_xdg_cache_home_wins_over_the_cli_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.client.cli.config import CLIConfig, save_config

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    save_config(CLIConfig(cache_home=str(tmp_path / "data" / "cache")))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert WorkerLock()._path == tmp_path / "xdg" / "lumiverb" / "worker.lock"


def test_worker_state_lives_beside_the_configured_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from rich.console import Console

    from src.client.cli import worker
    from src.client.cli.config import CLIConfig, save_config

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    save_config(CLIConfig(cache_home=str(tmp_path / "data" / "cache")))
    monkeypatch.setattr(worker, "LumiverbClient", MagicMock())
    monkeypatch.setattr(worker, "run_cycle", lambda client, *, state, **kw: state.last_full_scan.update(lib_1=1.0))
    worker.run_forever(once=True, console=Console(quiet=True))
    assert (tmp_path / "data" / "cache" / "lumiverb" / "worker-state.json").is_file()


def test_config_set_takes_the_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from src.client.cli.config import load_config
    from src.client.cli.main import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = CliRunner().invoke(app, ["config", "set", "--cache-home", "/mnt/ssd2/lumiverb/cache"])
    assert result.exit_code == 0, result.output
    assert load_config().cache_home == "/mnt/ssd2/lumiverb/cache"
