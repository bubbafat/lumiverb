"""lumiverb-server: look after this machine's Lumiverb server.

    lumiverb-server migrate [--env FILE]

migrate: the control plane's and every account's database migrations, as
scripts/migrate.sh does on the brain.

--env is the install's env file: $LUMIVERB_ENV_FILE, else /etc/lumiverb/env
(a Mac's Homebrew install passes its own). A variable already set in the
environment wins over the file's, as for the server's own settings.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src.server.lifecycle.envfile import EnvFileError, read_env_file
from src.server.lifecycle.migrate import MigrationError, migrate

DEFAULT_ENV_FILE = "/etc/lumiverb/env"


def install_env(path: Path) -> dict[str, str]:
    """The env file's variables under the environment's own. A missing file
    is fine when the environment has what's needed (a dev checkout)."""
    from_file = read_env_file(path) if path.is_file() else {}
    return {**from_file, **os.environ}


def _migrate(args: argparse.Namespace) -> int:
    env = install_env(args.env)
    url = env.get("CONTROL_PLANE_DATABASE_URL")
    if not url:
        print(f"✗ No CONTROL_PLANE_DATABASE_URL in {args.env} or the environment", file=sys.stderr)
        return 1
    try:
        count = migrate(url)
    except MigrationError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ Migrated the control plane and {count} account(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumiverb-server", description="Look after this machine's Lumiverb server.")
    parser.add_argument("--env", type=Path, default=Path(os.environ.get("LUMIVERB_ENV_FILE", DEFAULT_ENV_FILE)),
                        help="the install's env file (default: $LUMIVERB_ENV_FILE, else /etc/lumiverb/env)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="migrate the control plane and every account's database")
    args = parser.parse_args(argv)
    try:
        return {"migrate": _migrate}[args.command](args)
    except EnvFileError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


def entry() -> None:
    sys.exit(main())
