"""Whether this machine should hold back what can wait (ADR-018 decision 6):
a device's remakes wait for mains power and a network that isn't metered.

On battery: a Mac's pmset says which source it draws from; Linux says each
power supply in /sys/class/power_supply (a battery discharging). Metered:
NetworkManager says it of each connection on Linux (nmcli); a Mac says
nothing a command can read, so a Mac's network is never taken for metered.
Whatever can't be told (no battery, no NetworkManager, a command that
fails) goes on: nothing is held back on a guess.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

BATTERY, METERED = "battery", "metered"
SUPPLIES = Path("/sys/class/power_supply")


def held() -> str | None:
    """Why remakes wait now: BATTERY, METERED, or None (they go on)."""
    if on_battery():
        return BATTERY
    if metered():
        return METERED
    return None


def on_battery() -> bool:
    if sys.platform == "darwin":
        return battery_in_pmset(_output(["pmset", "-g", "ps"]))
    if sys.platform.startswith("linux"):
        return battery_in_supplies(SUPPLIES)
    return False


def metered() -> bool:
    if sys.platform.startswith("linux"):
        return metered_in_nmcli(_output(["nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.METERED", "device", "show"]))
    return False


def battery_in_pmset(text: str) -> bool:
    """pmset -g ps: "Now drawing from 'Battery Power'" (or 'AC Power')."""
    return "'Battery Power'" in text


def battery_in_supplies(root: Path) -> bool:
    """A battery among the power supplies, discharging (a desktop has none)."""
    try:
        supplies = list(root.iterdir())
    except OSError:
        return False
    for supply in supplies:
        if _read(supply / "type") == "Battery" and _read(supply / "status") == "Discharging":
            return True
    return False


def metered_in_nmcli(text: str) -> bool:
    """nmcli -t -f GENERAL.STATE,GENERAL.METERED device show: each device's
    state then whether it's metered ("yes", or "yes (guessed)" for a phone's
    hotspot): metered when a connected one is."""
    connected = False
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key == "GENERAL.STATE":
            connected = "(connected)" in value
        elif key == "GENERAL.METERED" and connected and value.startswith("yes"):
            return True
    return False


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _output(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("%s: %s", command[0], exc)
        return ""
