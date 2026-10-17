"""Where the scheduler's caches live: ~/.cache/lumiverb, or $XDG_CACHE_HOME/lumiverb.

An install sets XDG_CACHE_HOME to its data disk, so caches don't fill the
root disk during the first ingest. A client keeps none (ADR-020 decision 7).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.processing.cache_dir import cache_dir
from src.processing.proxy.analysis_cache import AnalysisProxyCache
from src.processing.proxy.proxy_cache import ProxyCache
from src.server.scheduler.scans import STATE_FILE, ServiceLock

pytestmark = pytest.mark.fast


def test_caches_follow_xdg_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert ProxyCache().path == tmp_path / "xdg" / "lumiverb" / "proxies"
    assert AnalysisProxyCache(object(), max_bytes=1).path_for("a").parent == tmp_path / "xdg" / "lumiverb" / "analysis"
    assert ServiceLock()._path == tmp_path / "xdg" / "lumiverb" / "worker.lock"
    assert cache_dir(STATE_FILE) == tmp_path / "xdg" / "lumiverb" / "worker-state.json"


def test_caches_default_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ProxyCache().path == tmp_path / ".cache" / "lumiverb" / "proxies"
    assert AnalysisProxyCache(object(), max_bytes=1).path_for("a").parent == tmp_path / ".cache" / "lumiverb" / "analysis"


def test_the_cli_has_no_cache_folder_of_its_own_to_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A client keeps no caches (ADR-020 decision 7): the CLI's --cache-home,
    which pointed a Linux install's CLI at the scheduler's, is gone."""
    from typer.testing import CliRunner

    from src.client.cli.main import app

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = CliRunner().invoke(app, ["config", "set", "--cache-home", "/mnt/ssd2/lumiverb/cache"])
    assert result.exit_code != 0 and "No such option" in result.output


def test_an_older_config_naming_a_cache_folder_still_loads_and_loses_it_when_saved(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from src.client.cli.config import load_config, save_config

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".lumiverb").mkdir()
    (tmp_path / ".lumiverb" / "config.json").write_text(json.dumps(
        {"api_url": "http://server:8000", "api_key": "k", "cache_home": "/data/cache", "root_map": {"/a": "/b"}}))
    cfg = load_config()
    assert (cfg.api_url, cfg.root_map) == ("http://server:8000", {"/a": "/b"})
    save_config(cfg)
    assert "cache_home" not in json.loads((tmp_path / ".lumiverb" / "config.json").read_text())


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
        lambda: proxies.get_source_hash(asset_id),
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
