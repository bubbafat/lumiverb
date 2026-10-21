"""What a person types that isn't shown: a key, pasted at a prompt or piped
in, kept off the command line, so out of the shell's history."""

from __future__ import annotations

import getpass
import sys


def at_a_terminal() -> bool:
    return sys.stdin.isatty()


def key_typed(what: str) -> str:
    """At a terminal, asked for and not shown (Enter ends it, as a paste
    does); piped in, all of it."""
    if at_a_terminal():
        return getpass.getpass(f"{what} (not shown; paste it and press Enter): ").strip()
    return sys.stdin.read().strip()
