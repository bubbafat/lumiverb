"""`lumiverb-server plugins`: the plug-ins installed on this server (ADR-023
phase 1a), the server admin's, on the machine itself (as server-admins).

  plugins                                 what's installed, and Lumiverb's own
  plugins install FOLDER --on|--off       install it (or a newer version), on or off by default
  plugins default ID on|off               its default, in every account that hasn't chosen
  plugins uninstall ID --keep|--trash|--delete
                                          no longer offered or run; what it made kept,
                                          in the trash for 30 days, or deleted now

Each shows what it reads and reaches (and an acknowledgment it asks, on by
default), or for an uninstall what it has and what stops working, and asks
before doing it; --yes is for no one to ask, having been shown it. Uninstall
asks which of its three when none is said, and refuses to guess when no one
can be asked.
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
    remove = actions.add_parser("uninstall", help="no longer offered or run; what it made kept, trashed or deleted")
    env_option(remove, argparse.SUPPRESS)
    remove.add_argument("plugin_id", help="its id")
    kept = remove.add_mutually_exclusive_group()
    kept.add_argument("--keep", dest="kept", action="store_const", const="keep",
                      help="its facts stay shown and searched, its settings and data kept: installing it again "
                           "picks it all up")
    kept.add_argument("--trash", dest="kept", action="store_const", const="trash",
                      help="hidden, kept 30 days, then deleted; installing it again before then brings it back")
    kept.add_argument("--delete", dest="kept", action="store_const", const="delete",
                      help="what it made, its settings and its own data, deleted now from every account")
    remove.add_argument("--yes", action="store_true", help="don't ask (having been shown what it has)")


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


_CHOICES = {"k": "keep", "keep": "keep", "t": "trash", "trash": "trash", "d": "delete", "delete": "delete"}
_DONE = {"keep": "what it made is kept, shown and searched as before: install it again to pick it all up",
         "trash": "what it made is in the trash, hidden: install it again within {days} days to bring it back",
         "delete": "what it made, its settings and its own data are deleted"}


def _uninstall(args: argparse.Namespace, session, data: Path) -> int:
    from src.server import plugin_install

    found = plugin_install.impact(args.plugin_id, session=session, data=data)
    print(f"Uninstalling {found.title} ({args.plugin_id}).")
    if found.accounts:
        print("It has:")
        for a in found.accounts:
            has = [f"facts for {a['clips']:,} clip{'' if a['clips'] == 1 else 's'}"] if a["clips"] else []
            has += ["its settings chosen"] if a["settings"] else []
            has += ["turned on or off there"] if a["chosen"] and not has else []
            print(f"  • {a['name']}: {', '.join(has)}")
    else:
        print("It has made nothing in any account.")
    for name in found.unreadable:
        print(f"  ! {name}: its database can't be read now, so what it has there isn't known")
    if found.data_mb:
        print(f"  • Its own data: {found.data_mb:,} MB")
    if found.stops:
        print("Without it:")
        for line in found.stops:
            print(f"  • {line}")
    kept = args.kept
    if kept is None:
        if args.yes or not sys.stdin.isatty():
            print("✗ Say what becomes of what it made: --keep, --trash or --delete", file=sys.stderr)
            return 1
        print(f"What it made:\n  [k]eep: shown and searched as before; install it again to pick it all up"
              f"\n  [t]rash: hidden, deleted in {plugin_install.TRASH_DAYS} days unless it's installed again"
              f"\n  [d]elete: what it made, its settings and its own data, deleted now")
        try:
            kept = _CHOICES.get(input("Keep, trash or delete? [k/t/d, anything else: cancel] ").strip().lower())
        except EOFError:
            kept = None
        if kept is None:
            print("Not uninstalled")
            return 1
    elif not _yes(f"Uninstall it, and {kept} what it made?", args.yes):
        print("Not uninstalled")
        return 1
    unreadable = plugin_install.uninstall(args.plugin_id, kept, data=data, session=session, by=_by(), now=utcnow())
    print(f"✓ Uninstalled {args.plugin_id}: {_DONE[kept].format(days=plugin_install.TRASH_DAYS)}")
    for name in unreadable:
        print(f"  ! {name}: its database couldn't be read, so what it made there stays, hidden")
    return 0


def _list(session) -> int:
    from src.server.repository.installed_plugins import InstalledPluginRepository

    rows = InstalledPluginRepository(session).all()
    if not rows:
        print("No plug-ins are installed: install one with `lumiverb-server plugins install FOLDER --on|--off`")
    else:
        from src.server.plugin_catalog import left_out

        left = {o["plugin_id"]: o["why"] for o in left_out(rows)}
        print("Installed plug-ins:")
        for row in rows:
            title = row["manifest"].get("title", "")
            print(f"  {row['plugin_id']} {row['version']}  {title}: "
                  f"{'on' if row['on_by_default'] else 'off'} by default")
            if row["plugin_id"] in left:
                print(f"    ! left out: {left[row['plugin_id']]}")
    from src.server.own_plugins import status

    for own in status(session):
        said = (f"installed ({own['installed']})" if own["installed"] == own["shipped"]
                else f"{own['installed']} installed, {own['shipped']} at the next start" if own["installed"]
                else "uninstalled by the server admin" if own["uninstalled"]
                else f"not installed: {own['failed']}" if own["failed"] else "not installed yet")
        print(f"Lumiverb's own: {own['plugin_id']} ({own['title']}) {said}")
    from src.server.repository.installed_plugins import InstalledPluginRepository as _Repo

    for row in _Repo(session).removed():
        title = row["manifest"].get("title", "")
        said = ("uninstalled, what it made kept" if row["kept"] == "keep"
                else f"uninstalled, in the trash until {row['purge_after']:%Y-%m-%d}")
        print(f"  {row['plugin_id']} {row['version']}  {title}: {said}")
    from src.server.plugin_install import EXAMPLE

    if EXAMPLE.is_dir() and not any(row["plugin_id"] == "example" for row in rows):
        print(f"To try Settings › Plug-ins, install Lumiverb's example: lumiverb-server plugins install {EXAMPLE} --off")
    return 0


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
