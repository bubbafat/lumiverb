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
