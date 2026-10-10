"""Where Lumiverb's local caches live: ~/.cache/lumiverb, or $XDG_CACHE_HOME/lumiverb.

The brain's scheduler sets XDG_CACHE_HOME to its data disk, so proxy caches
don't fill the root disk during the first ingest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.server.scheduler.scans import STATE_FILE, ServiceLock
from src.client.proxy.analysis_cache import AnalysisProxyCache
from src.client.proxy.proxy_cache import ProxyCache

pytestmark = pytest.mark.fast


def test_caches_follow_xdg_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert ProxyCache().path == tmp_path / "xdg" / "lumiverb" / "proxies"
    assert AnalysisProxyCache(object(), max_bytes=1).path_for("a").parent == tmp_path / "xdg" / "lumiverb" / "analysis"
    assert ServiceLock()._path == tmp_path / "xdg" / "lumiverb" / "worker.lock"


def test_caches_default_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ProxyCache().path == tmp_path / ".cache" / "lumiverb" / "proxies"
    assert AnalysisProxyCache(object(), max_bytes=1).path_for("a").parent == tmp_path / ".cache" / "lumiverb" / "analysis"


def test_caches_follow_the_cli_config_without_xdg_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A command run as the service's user without XDG_CACHE_HOME must still
    # find the service's lock, so two schedulers never run at once.
    from src.client.cli.config import CLIConfig, save_config

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    save_config(CLIConfig(cache_home=str(tmp_path / "data" / "cache")))
    assert ServiceLock()._path == tmp_path / "data" / "cache" / "lumiverb" / "worker.lock"
    assert ProxyCache().path == tmp_path / "data" / "cache" / "lumiverb" / "proxies"


def test_xdg_cache_home_wins_over_the_cli_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.client.cli.config import CLIConfig, save_config

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    save_config(CLIConfig(cache_home=str(tmp_path / "data" / "cache")))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert ServiceLock()._path == tmp_path / "xdg" / "lumiverb" / "worker.lock"


def test_scan_state_lives_beside_the_configured_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.client.cache_dir import cache_dir
    from src.client.cli.config import CLIConfig, save_config

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    save_config(CLIConfig(cache_home=str(tmp_path / "data" / "cache")))
    assert cache_dir(STATE_FILE) == tmp_path / "data" / "cache" / "lumiverb" / "worker-state.json"


def test_config_set_takes_the_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from src.client.cli.config import load_config
    from src.client.cli.main import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = CliRunner().invoke(app, ["config", "set", "--cache-home", "/mnt/ssd2/lumiverb/cache"])
    assert result.exit_code == 0, result.output
    assert load_config().cache_home == "/mnt/ssd2/lumiverb/cache"


def _config_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    from typer.testing import CliRunner

    from src.client.cli.main import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    return CliRunner().invoke(app, ["config", "set", *args])


def test_config_set_expands_a_cache_home_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.client.cli.config import load_config

    result = _config_set(tmp_path, monkeypatch, "--cache-home", "~/data/cache/")
    assert result.exit_code == 0, result.output
    assert load_config().cache_home == str(tmp_path / "data" / "cache")


@pytest.mark.parametrize("given", ["cache", "./data/cache", "~nosuchuser_lumiverb/cache"])
def test_config_set_refuses_a_relative_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: str) -> None:
    # Relative to wherever the command ran: the scheduler would look elsewhere.
    from src.client.cli.config import CLIConfig, load_config, save_config

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    save_config(CLIConfig(cache_home="/mnt/ssd2/lumiverb/cache"))
    result = _config_set(tmp_path, monkeypatch, "--cache-home", given, "--api-url", "http://elsewhere:9999")
    assert result.exit_code != 0
    assert "absolute" in result.output and given in result.output
    cfg = load_config()
    assert (cfg.cache_home, cfg.api_url) == ("/mnt/ssd2/lumiverb/cache", CLIConfig().api_url)


def test_config_set_clears_the_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.client.cli.config import CLIConfig, load_config, save_config

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    save_config(CLIConfig(cache_home="/mnt/ssd2/lumiverb/cache"))
    result = _config_set(tmp_path, monkeypatch, "--cache-home", "")
    assert result.exit_code == 0, result.output
    assert load_config().cache_home == ""


_BAD_IDS = ["../evil", "a/b", "..", ".", "", "a.b", "/etc/passwd", "a\\b", "a\nb", "a b"]


@pytest.mark.parametrize("asset_id", _BAD_IDS)
def test_cache_paths_refuse_ids_that_are_not_plain_names(tmp_path: Path, asset_id: str) -> None:
    proxies = ProxyCache()
    proxies._dir = tmp_path
    analysis = AnalysisProxyCache(object(), cache_dir=tmp_path, max_bytes=1)

    for build in (
        lambda: proxies.put(asset_id, b"x"),
        lambda: proxies.put_scan(asset_id, b"x", "sha"),
        lambda: proxies.get(asset_id),
        lambda: proxies.has(asset_id),
        lambda: proxies.get_sha(asset_id),
        lambda: proxies.remove(asset_id),
        lambda: analysis.path_for(asset_id),
    ):
        with pytest.raises(ValueError):
            build()
    assert list(tmp_path.iterdir()) == []


def test_cache_paths_take_real_ids(tmp_path: Path) -> None:
    proxies = ProxyCache()
    proxies._dir = tmp_path
    proxies.put_scan("ast_01H-x9", b"x", "sha")
    assert proxies.get("ast_01H-x9") == b"x"
    assert proxies.sha_path("ast_01H-x9") == tmp_path / "ast_01H-x9.sha"
    assert AnalysisProxyCache(object(), cache_dir=tmp_path, max_bytes=1).path_for("ast_1") == tmp_path / "ast_1.mp4"
