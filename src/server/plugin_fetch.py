"""A plug-in to install from a .zip: one on this machine, or one at an https
URL (Robert, 2026-10-20: "download and then do the local path mode").

The download is HTTPS only (redirects too), at most MAX_BYTES, and its
SHA-256 is said, and checked when the server admin gives one (`--sha256`).
The .zip is unpacked into a folder of its own before anything else looks at
it, refusing what a plug-in's folder never needs and an archive could use
against the machine: a name outside the folder (absolute, `..`, a drive or
a backslash), a link, a device or anything but files and folders, more than
MAX_FILES files or MAX_BYTES unpacked (whatever it says of itself). A file
the archive marks executable stays executable (a plug-in's `run` may be a
program in its folder). The folder holding lumiverb-plugin.json, at the
top or in the one folder at the top (as a release's .zip has it), is then
installed as any folder is (plugin_install.plan): nothing of it runs before
its manifest is read and accepted.
"""

from __future__ import annotations

import hashlib
import stat
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import httpx

from src.plugins.manifest import MANIFEST
from src.server.plugin_install import MAX_BYTES, MAX_FILES, PluginInstallError

DOWNLOAD_TIMEOUT_SEC = 60.0  # between bytes: a large download takes as long as it takes


def is_url(source: str) -> bool:
    return source.lower().startswith(("https://", "http://"))


def download(url: str, dest: Path, *, client: httpx.Client | None = None) -> str:
    """The .zip at an https URL saved to dest; its SHA-256. PluginInstallError when it's not https (a redirect
    neither), not there, or larger than a plug-in is."""
    if not url.lower().startswith("https://"):
        raise PluginInstallError(f"Only an https URL: {url[:100]}")
    digest = hashlib.sha256()
    size = 0
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SEC)
    try:
        with client.stream("GET", url) as r:
            if r.url.scheme != "https":
                raise PluginInstallError(f"It redirected to a URL that isn't https: {str(r.url)[:100]}")
            if r.status_code != 200:
                raise PluginInstallError(f"{url[:100]} answered HTTP {r.status_code}")
            with dest.open("wb") as out:
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise PluginInstallError(f"{url[:100]} is larger than a plug-in is ({MAX_BYTES // 1024**3} GB)")
                    digest.update(chunk)
                    out.write(chunk)
    except httpx.HTTPError as exc:
        raise PluginInstallError(f"Couldn't download {url[:100]}: {exc}") from exc
    finally:
        if own:
            client.close()
    return digest.hexdigest()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _member(info: zipfile.ZipInfo) -> PurePosixPath | None:
    """Where an entry goes in the folder; None for a folder entry. PluginInstallError for one that's no file."""
    name = info.filename
    path = PurePosixPath(name)
    if "\\" in name or path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]) \
            or not path.parts:
        raise PluginInstallError(f"The .zip holds a name outside its folder: {name[:100]!r}")
    kind = stat.S_IFMT(info.external_attr >> 16)
    if info.is_dir():
        return None
    if kind not in (0, stat.S_IFREG):  # 0: made where Unix modes aren't kept, a file
        raise PluginInstallError(f"The .zip holds something that isn't a file or a folder: {name[:100]!r}")
    return path


def unpack(archive: Path, folder: Path) -> Path:
    """The .zip unpacked into folder (made, empty), checked first; the folder holding its manifest.
    PluginInstallError saying what's wrong."""
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PluginInstallError(f"{archive.name} isn't a .zip: {exc}") from exc
    with zf:
        infos = zf.infolist()
        files = [(info, path) for info in infos if (path := _member(info)) is not None]
        if len(files) > MAX_FILES or sum(info.file_size for info, _ in files) > MAX_BYTES:
            raise PluginInstallError(f"{archive.name} holds more than a plug-in does ({MAX_FILES:,} files or "
                                     f"{MAX_BYTES // 1024**3} GB): is it the right one?")
        written = 0
        for info, path in files:
            dest = folder.joinpath(*path.parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, dest.open("wb") as out:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    written += len(chunk)
                    if written > MAX_BYTES:  # what it said of its sizes isn't so
                        raise PluginInstallError(f"{archive.name} unpacks to more than a plug-in is")
                    out.write(chunk)
            dest.chmod(0o755 if (info.external_attr >> 16) & 0o111 else 0o644)
    if (folder / MANIFEST).is_file():
        return folder
    tops = [p for p in folder.iterdir()]
    if len(tops) == 1 and tops[0].is_dir() and (tops[0] / MANIFEST).is_file():
        return tops[0]
    raise PluginInstallError(f"{archive.name} has no {MANIFEST} at its top, or in the one folder at its top")


def fetched(source: str, work: Path, *, sha256: str | None = None,
            say: Callable[[str], None] = print, client: httpx.Client | None = None) -> Path:
    """The folder to install from source: a folder as it is, or a .zip (a path, or an https URL) unpacked into
    work. With sha256, the .zip must be that (refused otherwise, before it's unpacked)."""
    if not is_url(source) and not source.lower().endswith(".zip"):
        if sha256:
            raise PluginInstallError("--sha256 is a .zip's: a folder has none")
        return Path(source)
    archive = work / "plugin.zip"
    if is_url(source):
        say(f"Downloading {source}")
        found = download(source, archive, client=client)
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise PluginInstallError(f"No .zip at {path}")
        archive, found = path, sha256_of(path)
    say(f"SHA-256 {found}")
    if sha256 and sha256.strip().lower() != found:
        raise PluginInstallError(f"Its SHA-256 is {found}, not the {sha256.strip().lower()} given: not installed")
    unpacked = work / "unpacked"
    unpacked.mkdir()
    return unpack(archive, unpacked)
