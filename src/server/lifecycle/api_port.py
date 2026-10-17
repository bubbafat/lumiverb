"""Where a Mac's API listens, on this machine alone: API_PORT, 6364 for an
install made now (init.py), out of the way of the ports devices use (6464
plain HTTP, 6465 HTTPS: network.py).

An install made before had the API on 6465. run's first start moves it,
once, as roles.py moved the databases' role: the env file's API_PORT (and
WEB_TLS_PORT, where it still says the 6443 v0.1.52-v0.1.53 wrote) and
nginx's two proxy_pass lines (init's own) rewritten, each file backed up
beside itself first (.before-ports-<time>). A config whose lines aren't
init's (changed by hand), or 6364 in use already, leaves both as they are,
and says so.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

API_PORT = "6364"
OLD_API_PORT = "6465"
OLD_TLS_PORT = "6443"  # v0.1.52-v0.1.53's HTTPS, before 6465 was free for it
TLS_PORT = "6465"


def _upstream(port: str) -> str:
    return f"proxy_pass http://127.0.0.1:{port};"  # init.py nginx_site's


def _rewrite(path: Path, text: str) -> None:
    part = path.with_name(path.name + ".part")
    part.write_text(text)
    part.chmod(path.stat().st_mode & 0o777)
    part.replace(path)


def moved(env_file: Path, env: dict[str, str], *, now: datetime | None = None,
          free: Callable[[int], bool] | None = None) -> tuple[dict[str, str], str | None]:
    """The env with the API moved to 6364 if it was a Mac's 6465 (its own
    PostgreSQL and nginx: PG_BIN, NGINX_CONFIG), and what was done, for the
    log: None when nothing was to be done."""
    if env.get("API_PORT") != OLD_API_PORT or not env.get("PG_BIN") or not env.get("NGINX_CONFIG"):
        return env, None
    from src.server.lifecycle import network

    config = Path(env["NGINX_CONFIG"])
    try:
        site = config.read_text()
    except OSError as exc:
        return env, f"The API stays on {OLD_API_PORT}: can't read {config} ({exc.strerror or exc})"
    if _upstream(OLD_API_PORT) not in site:
        return env, f"The API stays on {OLD_API_PORT}: {config} doesn't pass /v1/ there as init wrote it"
    if not (free or network.free)(int(API_PORT)):
        return env, f"The API stays on {OLD_API_PORT}: something else on this Mac is using {API_PORT}"

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    new = {"API_PORT": API_PORT, **({"WEB_TLS_PORT": TLS_PORT} if env.get("WEB_TLS_PORT") == OLD_TLS_PORT else {})}
    lines = []
    for line in env_file.read_text().splitlines():
        name, _, value = line.partition("=")
        name = name.strip()
        if not line.lstrip().startswith("#") and name in new and value.strip() == env[name]:
            line = f"{name}={new[name]}"
        lines.append(line)
    for path in (env_file, config):
        shutil.copy2(path, path.with_name(f"{path.name}.before-ports-{stamp}"))
    _rewrite(config, site.replace(_upstream(OLD_API_PORT), _upstream(API_PORT)))
    _rewrite(env_file, "\n".join(lines) + "\n")
    https = f", HTTPS's default from {OLD_TLS_PORT} to {TLS_PORT}" if "WEB_TLS_PORT" in new else ""
    return {**env, **new}, (f"The API moved from port {OLD_API_PORT} to {API_PORT}{https} "
                            f"({env_file} and {config}; each one before, beside it)")
