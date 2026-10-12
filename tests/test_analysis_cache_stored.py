"""The scheduler reads the analysis proxies the API stores on this machine
where they are (src/processing/proxy/analysis_cache.py `stored`,
src/server/scheduler/service.py stored_analysis_proxy): its cache holds no
second copy (on Robert's Mac, up to 50 GB again). A clip whose stored file
isn't here (none yet, a drive unplugged) is downloaded as before."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from src.processing.proxy.analysis_cache import AnalysisProxyCache


class _Resp:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status_code = status
        self._body = body

    def iter_bytes(self, chunk_size: int = 65536):
        yield self._body


class _Client:
    """The API's download of a clip's analysis proxy: what was asked for."""

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.calls: list[str] = []

    @contextmanager
    def stream(self, path: str, **kwargs: object):
        self.calls.append(path)
        asset_id = path.split("/")[3]
        yield _Resp(200, self.bodies[asset_id]) if asset_id in self.bodies else _Resp(404)


@pytest.mark.fast
def test_a_stored_proxy_is_read_where_it_is_with_no_copy(tmp_path):
    stored = tmp_path / "previews" / "ast_1_analysis.mp4"
    stored.parent.mkdir()
    stored.write_bytes(b"the server's")
    client = _Client({"ast_1": b"downloaded"})
    cache = AnalysisProxyCache(client, cache_dir=tmp_path / "cache", stored=lambda a: stored if a == "ast_1" else None)
    assert cache.get("ast_1") == stored
    assert client.calls == [] and not (tmp_path / "cache").exists()


@pytest.mark.fast
def test_a_copy_cached_before_goes_once_the_stored_one_is_read(tmp_path):
    stored = tmp_path / "ast_1.mp4"
    stored.write_bytes(b"the server's")
    cache = AnalysisProxyCache(_Client({}), cache_dir=tmp_path / "cache", stored=lambda a: stored)
    old = cache.path_for("ast_1")
    old.parent.mkdir(parents=True)
    old.write_bytes(b"a copy from before")
    assert cache.get("ast_1") == stored
    assert not old.exists(), "the duplicate goes"


@pytest.mark.fast
def test_one_not_stored_here_is_downloaded_as_before(tmp_path):
    client = _Client({"ast_2": b"downloaded"})
    cache = AnalysisProxyCache(client, cache_dir=tmp_path / "cache", stored=lambda a: None)
    got = cache.get("ast_2")
    assert got == cache.path_for("ast_2") and got.read_bytes() == b"downloaded"


@pytest.mark.fast
def test_a_lookup_that_fails_is_a_download_not_an_error(tmp_path):
    def broken(asset_id: str):
        raise ConnectionError("the database isn't answering")

    cache = AnalysisProxyCache(_Client({"ast_3": b"downloaded"}), cache_dir=tmp_path / "cache", stored=broken)
    assert cache.get("ast_3").read_bytes() == b"downloaded"


@pytest.mark.fast
def test_a_render_saved_to_the_server_isnt_kept_twice(tmp_path):
    stored = tmp_path / "ast_4.mp4"
    stored.write_bytes(b"the server's")
    rendered = tmp_path / "work.rendering"
    rendered.write_bytes(b"rendered")
    cache = AnalysisProxyCache(_Client({}), cache_dir=tmp_path / "cache", stored=lambda a: stored)
    assert cache.put("ast_4", rendered) == stored
    assert not rendered.exists() and not (tmp_path / "cache").exists()
    # Without it here (another machine's server), the render is the cache's.
    other = tmp_path / "work2.rendering"
    other.write_bytes(b"rendered")
    elsewhere = AnalysisProxyCache(_Client({}), cache_dir=tmp_path / "cache2", stored=lambda a: None)
    assert elsewhere.put("ast_5", other).read_bytes() == b"rendered"
