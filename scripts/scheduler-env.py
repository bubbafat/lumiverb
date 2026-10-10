#!/usr/bin/env python3
"""The scheduler's settings in the env file (/etc/lumiverb/env): LUMIVERB_*,
which src/server/scheduler/settings.py reads. Standard library only: the
deploy scripts run it with the system's python3, as root.

    scheduler-env.py ENV_FILE [--from-cli-config CONFIG_JSON] [--root-map SRC=DST ...]

--from-cli-config: the scheduler read the service user's CLI config before
(~/.lumiverb/config.json); what it set there that the env file doesn't have
yet is carried over, once. A setting already in the env file is never
changed by it. --root-map adds or replaces one library root mapping (as
`lumiverb config map-root`). The file is rewritten in place (its owner and
mode stay); each line set is said.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# CLI config key -> (env name, the default the scheduler has without it)
CARRIED = {
    "analysis_proxy_encoder": ("LUMIVERB_ANALYSIS_PROXY_ENCODER", "libx264"),
    "analysis_proxy_decoder": ("LUMIVERB_ANALYSIS_PROXY_DECODER", "auto"),
    "gpu_decodes": ("LUMIVERB_GPU_DECODES", 1),
    "render_concurrency": ("LUMIVERB_RENDER_CONCURRENCY", 0),
    "analysis_cache_gb": ("LUMIVERB_ANALYSIS_CACHE_GB", 50.0),
    "face_batch_limit": ("LUMIVERB_FACE_BATCHES_PER_PROCESS", 20),
}
ROOT_MAP = "LUMIVERB_ROOT_MAP"


def _clean(path: str) -> str:
    return path.rstrip("/") or "/"


def _value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    env_path, args = Path(argv[0]), argv[1:]
    cli_config: Path | None = None
    maps: list[tuple[str, str]] = []
    while args:
        flag, value = args[0], args[1] if len(args) > 1 else None
        if value is None:
            print(f"{flag} needs a value", file=sys.stderr)
            return 2
        if flag == "--from-cli-config":
            cli_config = Path(value)
        elif flag == "--root-map" and "=" in value:
            src, dst = value.split("=", 1)
            if not src.startswith("/") or not dst.startswith("/"):
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
    if cli_config is not None and cli_config.is_file():
        try:
            config = json.loads(cli_config.read_text())
        except ValueError:
            print(f"Can't read {cli_config}; nothing carried over", file=sys.stderr)
            config = {}
        if ROOT_MAP not in have and config.get("root_map"):
            root_map = {_clean(k): _clean(v) for k, v in config["root_map"].items()}
            wanted[ROOT_MAP] = ""
        for key, (env, default) in CARRIED.items():
            if env not in have and key in config and config[key] != default:
                wanted[env] = str(config[key])
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
