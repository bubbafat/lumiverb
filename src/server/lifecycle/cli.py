"""lumiverb-server: look after this machine's Lumiverb server.

    lumiverb-server migrate --env FILE

migrate: the control plane's and every account's database migrations, as
scripts/migrate.sh does on the brain.

Which install is always said, never assumed: --env FILE, or
$LUMIVERB_ENV_FILE, or CONTROL_PLANE_DATABASE_URL already in the
environment (a dev checkout). On the brain /etc/lumiverb/env is prod's, so a
dev checkout run without one would migrate prod: it's refused instead. A
variable already in the environment wins over the file's, as for the
server's own settings.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src.server.lifecycle.envfile import EnvFileError, read_env_file
from src.server.lifecycle.migrate import MigrationError, migrate

NO_INSTALL = ("Say which install: --env FILE or $LUMIVERB_ENV_FILE, or CONTROL_PLANE_DATABASE_URL in the "
              "environment. None is assumed (on the brain, /etc/lumiverb/env is prod's).")


class InstallError(Exception):
    pass


def install_env(env_file: Path | None) -> dict[str, str]:
    """The named env file's variables under the environment's own. Raises
    InstallError when no install is named or the file can't be read."""
    path = env_file or (Path(os.environ["LUMIVERB_ENV_FILE"]) if os.environ.get("LUMIVERB_ENV_FILE") else None)
    if path is None:
        if not os.environ.get("CONTROL_PLANE_DATABASE_URL"):
            raise InstallError(NO_INSTALL)
        return dict(os.environ)
    try:
        from_file = read_env_file(path)
    except FileNotFoundError:
        raise InstallError(f"No env file at {path}") from None
    except OSError as exc:
        raise InstallError(f"Can't read {path}: {exc.strerror or exc}") from None
    return {**from_file, **os.environ}


def _migrate(args: argparse.Namespace) -> int:
    env = install_env(args.env)
    url = env.get("CONTROL_PLANE_DATABASE_URL")
    if not url:
        raise InstallError(f"No CONTROL_PLANE_DATABASE_URL in {args.env or os.environ.get('LUMIVERB_ENV_FILE')}")
    try:
        count = migrate(url)
    except MigrationError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ Migrated the control plane and {count} account(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumiverb-server", description="Look after this machine's Lumiverb server.")
    parser.add_argument("--env", type=Path, default=None,
                        help="the install's env file (or $LUMIVERB_ENV_FILE); none is assumed")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="migrate the control plane and every account's database")
    args = parser.parse_args(argv)
    try:
        return {"migrate": _migrate}[args.command](args)
    except (InstallError, EnvFileError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


def entry() -> None:
    sys.exit(main())
