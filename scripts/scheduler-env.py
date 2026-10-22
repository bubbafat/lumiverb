#!/usr/bin/env python3
"""The scheduler's settings in the env file (/etc/lumiverb/env): LUMIVERB_*,
which src/server/scheduler/settings.py reads. Standard library only: the
deploy scripts run it with the system's python3, as root.

    scheduler-env.py ENV_FILE [--from-cli-config CONFIG_JSON|-] [--root-map SRC=DST ...]

--from-cli-config: the scheduler read the service user's CLI config before
(~/.lumiverb/config.json); what it set there that the env file doesn't have
yet is carried over, once. `-` reads it from stdin: the deploy scripts read
it as the service user, whose file it is, and hand it over. A setting already
in the env file is never changed by it. That file is the service user's to
write and the env file root's, so only a value of the type its setting takes,
on one line, is carried (a whole number, a number, a name, a map of full
paths); anything else is left out, and said. --root-map adds or replaces one
library root mapping (as `lumiverb config map-root`). The file is rewritten
in place (its owner and mode stay); each line set is said.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

WHOLE, NUMBER, NAME = "a whole number", "a number", "a name"
# CLI config key -> (env name, the default the scheduler has without it, what it must be)
CARRIED = {
    "analysis_proxy_encoder": ("LUMIVERB_ANALYSIS_PROXY_ENCODER", "libx264", NAME),
    "analysis_proxy_decoder": ("LUMIVERB_ANALYSIS_PROXY_DECODER", "auto", NAME),
    "gpu_decodes": ("LUMIVERB_GPU_DECODES", 1, WHOLE),
    "render_concurrency": ("LUMIVERB_RENDER_CONCURRENCY", 0, WHOLE),
    "analysis_cache_gb": ("LUMIVERB_ANALYSIS_CACHE_GB", 50.0, NUMBER),
    "face_batch_limit": ("LUMIVERB_FACE_BATCHES_PER_PROCESS", 20, WHOLE),
}
ROOT_MAP = "LUMIVERB_ROOT_MAP"
# An encoder's or a decoder's name, as ffmpeg spells them: libx264, h264_nvenc, cuda, auto.
_NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")
# The same written as text (a config edited by hand): its bounds are the scheduler's, which says at its start
# which setting it can't run with.
_WHOLE = re.compile(r"-?[0-9]{1,9}")
_NUMBER = re.compile(r"-?[0-9]{1,9}(\.[0-9]{1,9})?")


def _clean(path: str) -> str:
    return path.rstrip("/") or "/"


def _value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def _carried(value: object, kind: str) -> str | None:
    """The env file's text for a CLI config value of `kind`, or None when it isn't one: never a bool for a
    number, never a line break or anything but the characters its kind has."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value if {WHOLE: _WHOLE, NUMBER: _NUMBER, NAME: _NAME}[kind].fullmatch(value) else None
    if kind == WHOLE and isinstance(value, int):
        return str(value)
    if kind == NUMBER and isinstance(value, (int, float)) and math.isfinite(value):
        return str(value)
    return None


def _full_path(path: object) -> bool:
    return isinstance(path, str) and path.startswith("/") and path.isprintable()


def _root_map(value: object) -> dict[str, str] | None:
    """The CLI config's root map, each side a full path on one line, or None when it isn't one."""
    if not isinstance(value, dict) or not all(_full_path(k) and _full_path(v) for k, v in value.items()):
        return None
    return {_clean(k): _clean(v) for k, v in value.items()}


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    env_path, args = Path(argv[0]), argv[1:]
    cli_config: str | None = None
    maps: list[tuple[str, str]] = []
    while args:
        flag, value = args[0], args[1] if len(args) > 1 else None
        if value is None:
            print(f"{flag} needs a value", file=sys.stderr)
            return 2
        if flag == "--from-cli-config":
            cli_config = value
        elif flag == "--root-map" and "=" in value:
            src, dst = value.split("=", 1)
            if not _full_path(src) or not _full_path(dst):
                print(f"--root-map needs absolute paths: {value}", file=sys.stderr)
                return 2
            maps.append((_clean(src), _clean(dst)))
        else:
            print(f"Unknown or malformed: {flag} {value}", file=sys.stderr)
            return 2
        args = args[2:]

    lines = env_path.read_text().splitlines() if env_path.exists() else []
    have = {line.split("=", 1)[0]: i for i, line in enumerate(lines)
            if "=" in line and not line.lstrip().startswith("#")}
    wanted: dict[str, str] = {}

    root_map: dict[str, str] = {}
    if ROOT_MAP in have:
        try:
            root_map = json.loads(_value(lines[have[ROOT_MAP]].split("=", 1)[1]) or "{}")
        except ValueError:
            print(f"{ROOT_MAP} in {env_path} isn't JSON; fix it by hand", file=sys.stderr)
            return 1
    given = None
    if cli_config == "-":
        cli_config, given = "the service user's CLI config", sys.stdin.read()
    elif cli_config is not None and Path(cli_config).is_file():
        given = Path(cli_config).read_text()
    if given and given.strip():
        try:
            config = json.loads(given)
        except ValueError:
            config = None
        if not isinstance(config, dict):
            print(f"Can't read {cli_config}; nothing carried over", file=sys.stderr)
            config = {}
        if ROOT_MAP not in have and config.get("root_map"):
            carried = _root_map(config["root_map"])
            if carried is None:
                print(f"root_map in {cli_config} isn't a map of full paths, each on one line: left out "
                      f"(set {ROOT_MAP} in {env_path} by hand)", file=sys.stderr)
            else:
                root_map = carried
                wanted[ROOT_MAP] = ""
        for key, (env, default, kind) in CARRIED.items():
            if env in have or key not in config or config[key] == default:
                continue
            carried = _carried(config[key], kind)
            if carried is None:
                print(f"{key} in {cli_config} isn't {kind} on one line: left out (set {env} in {env_path} "
                      "by hand)", file=sys.stderr)
            else:
                wanted[env] = carried
    for src, dst in maps:
        root_map = {k: v for k, v in root_map.items() if _clean(k) != src}
        root_map[src] = dst
        wanted[ROOT_MAP] = ""
    if ROOT_MAP in wanted:
        text = json.dumps(root_map, sort_keys=True)
        if "'" in text:
            print("A library root with a ' in it can't go in the env file; map it by hand", file=sys.stderr)
            return 1
        wanted[ROOT_MAP] = f"'{text}'"

    for env, value in wanted.items():
        line = f"{env}={value}"
        if env in have:
            lines[have[env]] = line
        else:
            lines.append(line)
        print(f"  {line}")
    if wanted:
        with open(env_path, "r+" if env_path.exists() else "w") as f:  # keeps its owner and mode
            f.seek(0)
            f.write("\n".join(lines) + "\n")
            f.truncate()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
