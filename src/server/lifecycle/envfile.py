"""The install's env file (/etc/lumiverb/env on Linux, Homebrew's
etc/lumiverb/env on a Mac), read as systemd reads an EnvironmentFile.

systemd loads it for a Linux install's services; launchd can't, so a Mac's
service reads it here. One KEY=VALUE per line; blank lines and lines
starting with # or ; are skipped. A value in single quotes is taken as it
is (deploy-api.sh writes LUMIVERB_ROOT_MAP='{"/Volumes/...": "/mnt/..."}');
in double quotes, a backslash escapes the next character.
"""

from __future__ import annotations

import re
from pathlib import Path

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class EnvFileError(ValueError):
    pass


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def read_env_file(path: Path) -> dict[str, str]:
    """The file's variables, in order. Raises EnvFileError naming a line it can't read."""
    variables: dict[str, str] = {}
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            raise EnvFileError(f"{path}, line {number}: not KEY=VALUE: {raw!r}")
        variables[key] = _unquote(value.strip())
    return variables
