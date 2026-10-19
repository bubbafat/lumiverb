"""`lumiverb-server plugins`: the plug-ins installed on this server (ADR-023
phase 1a), the server admin's, on the machine itself (as server-admins).

  plugins                                 what's installed, and Lumiverb's own
  plugins install FOLDER --on|--off       install it (or a newer version), on or off by default
  plugins default ID on|off               its default, in every account that hasn't chosen
  plugins uninstall ID                    no longer offered; its folder removed

Each shows what it reads and reaches (and an acknowledgment it asks, on by
default) and asks before doing it; --yes is for no one to ask, having been
shown it.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from src.plugins.manifest import ManifestError, parse
from src.shared.utils import utcnow


def add_parser(commands, env_option: Callable[[argparse.ArgumentParser, object], None]) -> None:
    parser = commands.add_parser(
        "plugins", help="the plug-ins installed on this server: install one, on or off by default, or uninstall it")
    env_option(parser, argparse.SUPPRESS)
    actions = parser.add_subparsers(dest="plugins_action")
    install = actions.add_parser("install", help="install a plug-in from its folder (or a newer version of it)")
    env_option(install, argparse.SUPPRESS)
    install.add_argument("folder", type=Path, help="the plug-in's folder, with its lumiverb-plugin.json")
    default = install.add_mutually_exclusive_group()
    default.add_argument("--on", dest="on", action="store_const", const=True,
                         help="on in every account that hasn't turned it off")
    default.add_argument("--off", dest="on", action="store_const", const=False,
                         help="off until an account's admin turns it on")
    install.add_argument("--yes", action="store_true", help="don't ask (having been shown what it does)")
    change = actions.add_parser("default", help="on or off by default, in every account that hasn't chosen")
    env_option(change, argparse.SUPPRESS)
    change.add_argument("plugin_id", help="its id")
    change.add_argument("state", choices=("on", "off"))
    change.add_argument("--yes", action="store_true", help="don't ask (having been shown its acknowledgment)")
    remove = actions.add_parser("uninstall", help="no longer offered to any account; its folder removed")
    env_option(remove, argparse.SUPPRESS)
    remove.add_argument("plugin_id", help="its id")
    remove.add_argument("--yes", action="store_true", help="don't ask")


def _yes(question: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _by() -> str:
    return f"{getpass.getuser()} on this machine"


def _show_acknowledgment(ack) -> None:
    print(f"\n{ack.title} (version {ack.version})\n")
    for paragraph in ack.text.split("\n\n"):
        print(f"  {paragraph}\n")
    for link in ack.links:
        print(f"  {link.label}: {link.url}")


def _install(args: argparse.Namespace, session, data: Path) -> int:
    from src.server import plugin_install

    p = plugin_install.plan(args.folder, session, data)
    on = p.on_by_default(args.on)
    m = p.manifest
    print(f"{m.title} {m.version}{f', by {m.author}' if m.author else ''} ({m.plugin_id})\n  {m.description}")
    if p.before is not None and not p.more:
        print(f"Installed now: version {p.before['version']}. This version reads and reaches nothing more.")
    elif p.before is not None:
        print(f"Installed now: version {p.before['version']}. This version also:")
        for line in p.more:
            print(f"  • {line}")
    print("It:")
    for line in m.described():
        print(f"  • {line}")
    where = "on in every account that hasn't turned it off" if on else "off until an account's admin turns it on"
    if p.asks and not _yes(f"Install it, {where}?", args.yes):
        print("Not installed")
        return 1
    acknowledged = False
    if p.acknowledgment_asked(on):
        _show_acknowledgment(m.acknowledgment)
        acknowledged = _yes("\nOn by default, you accept this for every account that hasn't chosen. Accept it?",
                            args.yes)
        if not acknowledged:
            print("Not installed: install it --off for each account's admin to accept it")
            return 1
    plugin_install.install(p, on_by_default=on, acknowledged=acknowledged, data=data, session=session, by=_by(),
                           now=utcnow())
    print(f"✓ Installed {m.plugin_id} {m.version}: {where}")
    return 0


def _default(args: argparse.Namespace, session) -> int:
    from src.server import plugin_install
    from src.server.repository.installed_plugins import InstalledPluginRepository

    row = InstalledPluginRepository(session).get(args.plugin_id)
    if row is None:
        raise plugin_install.PluginInstallError(f"No plug-in {args.plugin_id} is installed")
    on = args.state == "on"
    ack = parse(row["manifest"]).acknowledgment
    acknowledged = False
    if on and ack is not None:
        _show_acknowledgment(ack)
        acknowledged = _yes("\nOn by default, you accept this for every account that hasn't chosen. Accept it?",
                            args.yes)
        if not acknowledged:
            print("Not changed")
            return 1
    plugin_install.set_default(args.plugin_id, on, acknowledged=acknowledged, session=session, by=_by(),
                               now=utcnow())
    print(f"✓ {args.plugin_id} is {'on' if on else 'off'} by default, in every account that hasn't chosen")
    return 0


def _uninstall(args: argparse.Namespace, session, data: Path) -> int:
    from src.server import plugin_install

    if not _yes(f"Uninstall {args.plugin_id}? No account will have it, and its own data goes", args.yes):
        print("Not uninstalled")
        return 1
    plugin_install.uninstall(args.plugin_id, data=data, session=session, by=_by(), now=utcnow())
    print(f"✓ Uninstalled {args.plugin_id}")
    return 0


def _list(session) -> int:
    from src.server.repository.installed_plugins import InstalledPluginRepository

    rows = InstalledPluginRepository(session).all()
    if not rows:
        print("No plug-ins are installed: install one with `lumiverb-server plugins install FOLDER --on|--off`")
    else:
        print("Installed plug-ins:")
        for row in rows:
            title = row["manifest"].get("title", "")
            print(f"  {row['plugin_id']} {row['version']}  {title}: "
                  f"{'on' if row['on_by_default'] else 'off'} by default")
    from src.server.own_plugins import status

    for own in status(session):
        said = (f"installed ({own['installed']})" if own["installed"] == own["shipped"]
                else f"{own['installed']} installed, {own['shipped']} at the next start" if own["installed"]
                else "uninstalled by the server admin" if own["uninstalled"]
                else f"not installed: {own['failed']}" if own["failed"] else "not installed yet")
        print(f"Lumiverb's own: {own['plugin_id']} ({own['title']}) {said}")
    _still_named(session)
    from src.server.plugin_install import EXAMPLE

    if EXAMPLE.is_dir() and not any(row["plugin_id"] == "example" for row in rows):
        print(f"To try Settings › Plug-ins, install Lumiverb's example: lumiverb-server plugins install {EXAMPLE} --off")
    return 0


def _still_named(control) -> None:
    """Each account's clips still showing the old Place names' name (part 3: until the GeoNames plug-in's are
    saved; part 4 drops them once none do). Goes in part 4."""
    from sqlalchemy.exc import SQLAlchemyError

    from src.server.database import get_tenant_session
    from src.server.repository.asset_places import still_shown
    from src.server.repository.control_plane import TenantRepository

    for tenant in TenantRepository(control).list_all():
        if tenant.status != "active":
            continue
        try:
            with get_tenant_session(tenant.tenant_id) as session:
                n = still_shown(session)
        except SQLAlchemyError as exc:
            print(f"  {tenant.name} ({tenant.tenant_id}): can't be read ({str(exc).strip().splitlines()[0]})")
            continue
        print(f"  {tenant.name} ({tenant.tenant_id}): {n:,} clip{'' if n == 1 else 's'} still showing an old place "
              "name (the GeoNames plug-in names them)")


def run(args: argparse.Namespace, data: Path) -> int:
    """The command, with the install's settings in the environment already."""
    from sqlalchemy.exc import SQLAlchemyError

    from src.server.database import get_control_session
    from src.server.plugin_install import PluginInstallError

    try:
        with get_control_session() as session:
            if args.plugins_action == "install":
                return _install(args, session, data)
            if args.plugins_action == "default":
                return _default(args, session)
            if args.plugins_action == "uninstall":
                return _uninstall(args, session, data)
            return _list(session)
    except ManifestError as exc:
        print("✗ Its manifest isn't one Lumiverb takes:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  • {problem}", file=sys.stderr)
        return 1
    except PluginInstallError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"✗ Can't reach the control database: {first}", file=sys.stderr)
        return 1
