"""Starts a plug-in's program (ADR-023 phase 1b), as the plug-in runner asks:

    python -I plugin_launch.py FILE_SIZE_BYTES -- PROGRAM [ARGS...]

The standard library only, nothing of Lumiverb's: it runs in the plug-in's
process, before the plug-in's own program takes its place. It holds it to
what it may do on a Mac and on Linux alike: no file it writes passes its
size (a write past it fails, "File too large", rather than ending it), no
core files, and a lower priority than Lumiverb's own processes (nice 10);
then it runs the program, which keeps all three.
"""

import os
import resource
import signal
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        print("usage: plugin_launch.py FILE_SIZE_BYTES -- PROGRAM [ARGS...]", file=sys.stderr)
        return 2
    largest, program = int(argv[1]), argv[3:]
    resource.setrlimit(resource.RLIMIT_FSIZE, (largest, largest))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # ignored across exec: the write fails instead
    os.nice(10)
    try:
        os.execvp(program[0], program)
    except OSError as exc:
        print(f"Can't start {program[0]}: {exc.strerror or exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv))
