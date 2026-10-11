#!/usr/bin/env python3
"""The Homebrew formula's release: its url and sha256 set to a tagged
release's source archive (scripts/release.sh runs it on the tap's
Formula/lumiverb.rb). The first release adds them, after `homepage`; a later
one replaces them. Nothing else in the formula changes.

    bump_formula.py FORMULA URL SHA256
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_URL = re.compile(r'^  url "[^"]*"\n', re.MULTILINE)
_SHA = re.compile(r'^  sha256 "[0-9a-f]{64}"\n', re.MULTILINE)
_HOMEPAGE = re.compile(r'^  homepage "[^"]*"\n', re.MULTILINE)


class FormulaError(ValueError):
    pass


def bumped(formula: str, url: str, sha256: str) -> str:
    """The formula with its release's url and sha256 (only the formula's own:
    the first ones, before any `resource` or block)."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise FormulaError(f"Not a sha256: {sha256!r}")
    if '"' in url:
        raise FormulaError(f"Not a url: {url!r}")
    release = f'  url "{url}"\n  sha256 "{sha256}"\n'
    url_line, sha_line = _URL.search(formula), _SHA.search(formula)
    if url_line and sha_line:
        if sha_line.start() != url_line.end():
            raise FormulaError("The formula's url and sha256 aren't one after the other: bump it by hand")
        return formula[:url_line.start()] + release + formula[sha_line.end():]
    if url_line or sha_line:
        raise FormulaError("The formula has a url or a sha256 but not both: bump it by hand")
    homepage = _HOMEPAGE.search(formula)
    if not homepage:
        raise FormulaError("No homepage line to put the release after")
    return formula[:homepage.end()] + release + formula[homepage.end():]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    path, url, sha256 = Path(argv[0]), argv[1], argv[2]
    try:
        path.write_text(bumped(path.read_text(), url, sha256))
    except (OSError, FormulaError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
