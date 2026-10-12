"""Running ffmpeg, stopped only when it's stuck, never because it's slow.

ffmpeg reports how far it has got (-progress: frames, output time, size,
twice a second). One that hasn't moved for STALL_SEC is stopped. One that
moves, however slowly (a 5.3K original decoded on a busy machine's CPU),
finishes: a timeout from the clip's length killed renders a starved CPU
would have finished, ten minutes of work lost each time (Robert: a starved
CPU must never be penalized with a timeout).

Which decoder reads the original is the machine's: decoded_by() is
Vulkan where it works (a Linux GPU), else VideoToolbox (a Mac's media
engine), else the CPU. H.264 and HEVC decoding is exact: the pictures
are the same whichever decodes them, where the hardware decodes them at
all. One that fails on a picture is stopped there (hwaccel_args), and the
caller decodes on the CPU instead."""

from __future__ import annotations

import functools
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

STALL_SEC = 300.0
# Decoders that need no room on a GPU beside the models: a Mac's media
# engine shares the machine's memory. Every render may decode there at once.
SHARED_MEMORY = ("videotoolbox",)


class StalledError(Exception):
    """ffmpeg made no progress for the time given; it was stopped."""


@dataclass(frozen=True)
class Finished:
    returncode: int
    stderr: str


def run(cmd: list[str], *, stall_after: float = STALL_SEC) -> Finished:
    """Runs ffmpeg `cmd` (its first item the program) with progress
    reports; stops it and raises StalledError when nothing moved for
    `stall_after` seconds."""
    full = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    moved = [time.monotonic()]

    def watch(stdout) -> None:
        last: dict[str, str] = {}
        now: dict[str, str] = {}
        for raw in stdout:
            key, _, value = raw.decode(errors="replace").strip().partition("=")
            if key in ("frame", "out_time_us", "total_size"):
                now[key] = value
            elif key == "progress" and now != last:
                moved[0] = time.monotonic()
                last = dict(now)

    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(full, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err)
        reader = threading.Thread(target=watch, args=(proc.stdout,), name="ffmpeg-progress", daemon=True)
        reader.start()
        poll = min(1.0, stall_after / 4)
        while True:
            try:
                code = proc.wait(timeout=poll)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - moved[0] > stall_after:
                    proc.kill()
                    proc.wait()
                    reader.join(5)
                    raise StalledError(f"ffmpeg made no progress for {stall_after:.0f} s") from None
        reader.join(5)
        err.seek(0)
        return Finished(code, err.read().decode(errors="replace"))


@functools.cache
def _opens(device: str) -> bool:
    """Whether this ffmpeg can open a `device` hardware device here."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-init_hw_device", device,
             "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.04", "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, timeout=30, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def vulkan_decodes() -> bool:
    """Whether this ffmpeg can open a Vulkan device here. A device without
    video decoding (a software driver) is fine: ffmpeg then decodes on the CPU."""
    return _opens("vulkan")


def videotoolbox_decodes() -> bool:
    """Whether this ffmpeg can open VideoToolbox here (a Mac)."""
    return _opens("videotoolbox")


def hwaccel_args(hwaccel: str | None) -> list[str]:
    """ffmpeg's arguments for decoding on `hwaccel` (none: the CPU), before its -i.

    A hardware decoder can fail on some pictures of a stream it took on (a
    Mac's on a Sony 4:2:2 10-bit H.264 clip: about one in twenty), and
    ffmpeg then leaves them out, fills the gaps with the pictures before,
    and still finishes. -xerror stops it at the first error instead, so the
    caller makes it again on the CPU, which goes on past an error as it
    always did: the same pictures, every one of them."""
    return ["-xerror", "-hwaccel", hwaccel] if hwaccel else []


def decoded_by(choice: str) -> str | None:
    """The ffmpeg hwaccel to decode with, or None for the CPU: `choice` is
    "auto" (this machine's best), "cpu", or an ffmpeg hwaccel name."""
    if choice in ("", "cpu"):
        return None
    if choice == "auto":
        if vulkan_decodes():
            return "vulkan"
        return "videotoolbox" if videotoolbox_decodes() else None
    return choice
