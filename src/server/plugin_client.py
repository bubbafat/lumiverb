"""Asking a plug-in (ADR-023): HTTP over its Unix socket
(DATA_DIR/plugins/run/<id>.sock), each request with a time limit and its
answer read up to ANSWER_MAX_BYTES, whatever it sends. Lumiverb asks; a
plug-in only answers (it holds no key to Lumiverb).

A Unix socket's path is short (macOS: 104 bytes). A data folder too deep
for one (an external drive's, say) is reached through a link in /tmp, this
user's alone, made by whichever process asks first: on Linux the API and
the runner each have a /tmp of their own (PrivateTmp), each its own link to
the same folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import httpx

ANSWER_MAX_BYTES = 64 * 1024
SOCKET_PATH_MAX = 100  # bytes, below macOS's 104 with its end


def sockets_dir(data: Path) -> Path:
    """Every plug-in's socket, in one folder only Lumiverb's user can reach (the runner makes it)."""
    return data / "plugins" / "run"


def _short(folder: Path) -> Path:
    """A link in /tmp to folder: made if it isn't there; refused (OSError) if what's there isn't this user's link to
    it."""
    target = str(folder.absolute())
    link = Path("/tmp") / f"lumiverb-{os.getuid()}-{hashlib.sha256(target.encode()).hexdigest()[:12]}"
    for _ in range(2):
        try:
            st = os.lstat(link)
        except FileNotFoundError:
            try:
                os.symlink(target, link)
            except FileExistsError:
                continue  # another process made it meanwhile: looked at again
            return link
        if stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid() and os.readlink(link) == target:
            return link
        break
    raise OSError(f"{link} isn't Lumiverb's link to {target}: plug-ins' sockets can't be reached")


def socket_path(data: Path, plugin_id: str) -> Path:
    """Where a plug-in listens: DATA_DIR/plugins/run/<id>.sock, or through a short link when that's too long."""
    direct = sockets_dir(data) / f"{plugin_id}.sock"
    if len(os.fsencode(direct)) <= SOCKET_PATH_MAX:
        return direct
    return _short(sockets_dir(data)) / f"{plugin_id}.sock"


class PluginAnswerError(Exception):
    """It didn't answer as the protocol says: not listening, too slow, an error, or not JSON."""


def ask(sock: Path, method: str, path: str, *, body: dict[str, Any] | None = None,
        timeout: float = 5.0) -> dict[str, Any]:
    """Its answer (a JSON object, HTTP 200); PluginAnswerError with why otherwise."""
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)), timeout=timeout) as client, \
                client.stream(method, f"http://plugin{path}", json=body) as r:
            raw = b""
            for chunk in r.iter_bytes():
                raw += chunk
                if len(raw) > ANSWER_MAX_BYTES:
                    raise PluginAnswerError(f"its answer is over {ANSWER_MAX_BYTES // 1024} KB")
            if r.status_code != 200:
                raise PluginAnswerError(f"it answered HTTP {r.status_code}")
    except (httpx.HTTPError, OSError) as exc:
        raise PluginAnswerError(str(exc) or type(exc).__name__) from exc
    try:
        answer = json.loads(raw)
    except ValueError as exc:
        raise PluginAnswerError("its answer isn't JSON") from exc
    if not isinstance(answer, dict):
        raise PluginAnswerError("its answer isn't an object")
    return answer
