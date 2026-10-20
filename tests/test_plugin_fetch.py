"""A plug-in installed from a .zip, on this machine or at an https URL
(src/server/plugin_fetch.py): unpacked into a folder of its own before
anything looks at it, refusing what an archive could use against the
machine, then installed as a folder is. Downloads go through a transport of
the test's own: nothing reaches the internet."""

from __future__ import annotations

import hashlib
import stat
import zipfile
from pathlib import Path

import httpx
import pytest

from src.server import plugin_fetch
from src.server.plugin_fetch import fetched
from src.server.plugin_install import PluginInstallError

pytestmark = pytest.mark.fast

HELLO = Path(__file__).parent / "plugins" / "hello"


def _zip(path: Path, entries: dict[str, bytes | int], top: str = "") -> Path:
    """A .zip of name -> bytes (a file; mode 0o644) or an int (a raw mode for an empty entry)."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            info = zipfile.ZipInfo(top + name)
            if isinstance(content, int):
                info.external_attr = content << 16
                zf.writestr(info, b"")
            else:
                info.external_attr = (stat.S_IFREG | (0o755 if name.endswith(".sh") else 0o644)) << 16
                zf.writestr(info, content)
    return path


def _hello(tmp_path: Path, top: str = "") -> Path:
    files = {p.name: p.read_bytes() for p in HELLO.iterdir() if p.is_file()}
    return _zip(tmp_path / "hello.zip", {**files, "bin/start.sh": b"#!/bin/sh\n"}, top)


def _work(tmp_path: Path, name: str = "work") -> Path:
    """A fresh folder to unpack into."""
    work = tmp_path / name
    work.mkdir()
    return work


def test_a_zip_with_its_manifest_at_the_top_or_in_its_one_folder_is_its_folder(tmp_path):
    for top in ("", "hello-1.0/"):
        work = tmp_path / f"w{len(top)}"
        work.mkdir()
        said: list[str] = []
        folder = fetched(str(_hello(tmp_path, top)), work, say=said.append)
        assert (folder / "lumiverb-plugin.json").is_file() and (folder / "hello.py").is_file()
        assert folder.is_relative_to(work)
        assert said == [f"SHA-256 {hashlib.sha256((tmp_path / 'hello.zip').read_bytes()).hexdigest()}"]
        assert (folder / "bin" / "start.sh").stat().st_mode & 0o111, "kept executable"
        assert not (folder / "hello.py").stat().st_mode & 0o111


def test_a_folder_is_installed_as_it_is():
    assert fetched(str(HELLO), Path("/nowhere")) == HELLO
    with pytest.raises(PluginInstallError, match="a folder has none"):
        fetched(str(HELLO), Path("/nowhere"), sha256="ab")


@pytest.mark.parametrize("name", ["../evil.py", "/etc/evil", "dir/../../evil", "a\\..\\evil", "C:/evil"])
def test_a_name_outside_its_folder_is_refused_and_nothing_is_written(tmp_path, name):
    work = _work(tmp_path)
    with pytest.raises(PluginInstallError, match="outside its folder"):
        fetched(str(_zip(tmp_path / "bad.zip", {"lumiverb-plugin.json": b"{}", name: b"x"})), work)
    assert not (tmp_path / "evil.py").exists() and not (tmp_path / "evil").exists()


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFCHR | 0o644, stat.S_IFIFO | 0o644])
def test_a_link_or_anything_but_a_file_is_refused(tmp_path, mode):
    with pytest.raises(PluginInstallError, match="isn't a file or a folder"):
        fetched(str(_zip(tmp_path / "bad.zip", {"lumiverb-plugin.json": b"{}", "link": mode})), _work(tmp_path))


def test_more_than_a_plug_in_holds_is_refused(tmp_path, monkeypatch):
    archive = _hello(tmp_path)
    monkeypatch.setattr(plugin_fetch, "MAX_FILES", 2)
    with pytest.raises(PluginInstallError, match="holds more than a plug-in does"):
        fetched(str(archive), _work(tmp_path))
    monkeypatch.setattr(plugin_fetch, "MAX_FILES", 100)
    monkeypatch.setattr(plugin_fetch, "MAX_BYTES", 100)
    with pytest.raises(PluginInstallError, match="holds more than a plug-in does"):
        fetched(str(archive), _work(tmp_path, "w2"))


def test_a_zip_without_its_manifest_where_it_belongs_is_refused(tmp_path):
    with pytest.raises(PluginInstallError, match="has no lumiverb-plugin.json"):
        fetched(str(_zip(tmp_path / "a.zip", {"a/lumiverb-plugin.json": b"{}", "b/x": b"x"})), _work(tmp_path))
    with pytest.raises(PluginInstallError, match="isn't a .zip"):
        (tmp_path / "not.zip").write_bytes(b"plain text")
        fetched(str(tmp_path / "not.zip"), _work(tmp_path, "w2"))


def test_its_sha256_given_must_be_its_own(tmp_path):
    archive = _hello(tmp_path)
    own = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(PluginInstallError, match="not installed"):
        fetched(str(archive), _work(tmp_path), sha256="0" * 64)
    assert fetched(str(archive), _work(tmp_path, "w2"),
                   sha256=own.upper()).is_dir()


def _serving(routes: dict[str, httpx.Response]) -> tuple[httpx.Client, list[str]]:
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.Client(transport=httpx.MockTransport(answer), follow_redirects=True), asked


def test_a_zip_at_an_https_url_is_downloaded_then_installed_as_a_local_one(tmp_path):
    body = _hello(tmp_path).read_bytes()
    client, asked = _serving({
        "https://example.org/releases/latest": httpx.Response(302, headers={"Location": "https://cdn.example.org/h.zip"}),
        "https://cdn.example.org/h.zip": httpx.Response(200, content=body)})
    said: list[str] = []
    folder = fetched("https://example.org/releases/latest", _work(tmp_path), say=said.append, client=client,
                     sha256=hashlib.sha256(body).hexdigest())
    assert (folder / "lumiverb-plugin.json").is_file()
    assert asked == ["https://example.org/releases/latest", "https://cdn.example.org/h.zip"]
    assert said == ["Downloading https://example.org/releases/latest", f"SHA-256 {hashlib.sha256(body).hexdigest()}"]


def test_only_https_is_downloaded_redirects_too_and_what_isnt_there_or_is_too_big_is_refused(tmp_path, monkeypatch):
    body = _hello(tmp_path).read_bytes()
    client, asked = _serving({
        "https://example.org/to-http": httpx.Response(302, headers={"Location": "http://example.org/h.zip"}),
        "http://example.org/h.zip": httpx.Response(200, content=body),
        "https://example.org/h.zip": httpx.Response(200, content=body)})
    with pytest.raises(PluginInstallError, match="Only an https URL"):
        fetched("http://example.org/h.zip", _work(tmp_path), client=client)
    assert asked == [], "never asked"
    with pytest.raises(PluginInstallError, match="isn't https"):
        fetched("https://example.org/to-http", _work(tmp_path, "w2"),
                client=client)
    with pytest.raises(PluginInstallError, match="HTTP 404"):
        fetched("https://example.org/gone.zip", _work(tmp_path, "w3"),
                client=client)
    monkeypatch.setattr(plugin_fetch, "MAX_BYTES", 100)
    with pytest.raises(PluginInstallError, match="larger than a plug-in is"):
        fetched("https://example.org/h.zip", _work(tmp_path, "w4"),
                client=client)
