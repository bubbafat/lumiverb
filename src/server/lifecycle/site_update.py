"""What the web app's nginx site has gained since an install's was written, made to that one: deploy-web.sh writes
a Linux install's site once (certbot then adds its HTTPS), and init.py a Mac's whole config once, so a change to
what they write reaches an install only through here. update-web.sh runs it on a Linux site (`python3 -I
site_update.py SITE`: the standard library alone, as a web server may have no Lumiverb Python), and a Mac's run on
its own config at each start, before nginx starts (cli.py `_run`).

Each change is made where the text is as Lumiverb wrote it, and once: what's up to date already is left as it is,
and what was changed by hand there is left too, and said. What a site gains:
- video from blob: URLs (the grid's previews, playback's fallback), which the CSP blocked;
- the API's Referrer-Policy, and its Permissions-Policy beside the other headers;
- text compressed, and nginx's version unsaid;
- a hidden file (/.env, /.git/) or a file name the app doesn't have answered 404, never the app's page.

init.py and deploy-web.sh write these same lines (tests/test_site_update.py holds them to it). It runs on the
python3 of every Ubuntu deploy-web.sh supports: 3.10 at the oldest.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# The built app loads only its own files and calls only its own /v1/: no inline script or style, pictures and
# videos shown from blob: URLs, video streamed from /v1/stream/.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
       "frame-ancestors 'none'")
OLD_CSP = CSP.replace("media-src 'self' blob:;", "media-src 'self';")

REFERRER = "add_header Referrer-Policy strict-origin-when-cross-origin always;"
OLD_REFERRER = "add_header Referrer-Policy no-referrer-when-downgrade always;"
PERMISSIONS = 'add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;'

# Every response's headers: repeated in a location with add_header of its own, which drops the server's.
HEADERS = (
    "add_header X-Content-Type-Options nosniff always;",
    f'add_header Content-Security-Policy "{CSP}" always;',
    "add_header X-Frame-Options DENY always;",
    REFERRER,
    PERMISSIONS,
)

# After the server's headers.
DELIVERY = (
    "# Text goes compressed (the app's code and styles, the API's answers), and",
    "# nginx doesn't say its version.",
    "server_tokens off;",
    "gzip on;",
    "gzip_types text/css application/javascript text/javascript application/json image/svg+xml;",
    "gzip_min_length 1024;",
    "gzip_vary on;",
)

# The last location: the app's pages.
APP_PAGES = (
    "# The app's pages: any path that isn't one of its files. A hidden file",
    "# (/.env, /.git/) or a file name it doesn't have is a 404, never the page.",
    "location / {",
    "    location ~ /\\. {",
    "        return 404;",
    "    }",
    "    location ~ \\.[A-Za-z0-9]+$ {",
    "        try_files $uri =404;",
    "    }",
    "    try_files $uri $uri/ /index.html;",
    "}",
)
_OLD_APP_PAGES = ("location / {", "    try_files $uri $uri/ /index.html;", "}")

_SERVER = re.compile(r"(\s*)server\s*\{\s*")

GAINED = {
    "csp": "Video from blob: URLs plays (the CSP)",
    "referrer": "The referrer policy is the API's",
    "permissions": "The permissions policy is on",
    "delivery": "Text is compressed, and nginx doesn't say its version",
    "pages": "A hidden file or a file the app doesn't have is a 404, not the page",
}


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _server(block: list[str], gained: set[str], left: list[str]) -> list[str]:
    """One server block brought up to date. Its own directives are at its server_name's indentation; a block
    without one isn't the app's."""
    pad = next((_indent(line) for line in block if line.strip().startswith("server_name ")), None)
    if pad is None:
        return block
    delivered = any(line.strip() == "gzip on;" for line in block)
    paged = any(line.strip() == APP_PAGES[3].strip() for line in block)
    out: list[str] = []

    def permissions_said(at: str) -> None:
        nonlocal delivered
        if at == pad and not delivered:
            out.extend(["", *(pad + line for line in DELIVERY)])
            delivered = True
            gained.add("delivery")

    j = 0
    while j < len(block):
        line = block[j]
        new = line.replace(f'Content-Security-Policy "{OLD_CSP}"', f'Content-Security-Policy "{CSP}"')
        if new != line:
            gained.add("csp")
        if new.strip() == OLD_REFERRER:
            new = _indent(new) + REFERRER
            gained.add("referrer")
        if not paged and new == pad + _OLD_APP_PAGES[0]:
            if [*block[j:j + 3]] == [pad + part for part in _OLD_APP_PAGES]:
                out.extend(pad + part for part in APP_PAGES)
                gained.add("pages")
                j += 3
                continue
            left.append("Its location / isn't as Lumiverb wrote it: a hidden file or a file the app doesn't have "
                        "still gets the page")
        out.append(new)
        if new.strip() == REFERRER and (j + 1 >= len(block) or block[j + 1].strip() != PERMISSIONS):
            out.append(_indent(new) + PERMISSIONS)
            gained.add("permissions")
            permissions_said(_indent(new))
        elif new.strip() == PERMISSIONS:
            permissions_said(_indent(new))
        j += 1
    return out


def updated(text: str) -> tuple[str, list[str], list[str]]:
    """The site's text with what it has gained, what it gained (for the log), and what was left as it was."""
    lines = text.split("\n")
    gained: set[str] = set()
    left: list[str] = []
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _SERVER.fullmatch(lines[i])
        end = next((k for k in range(i + 1, len(lines)) if lines[k] == f"{m.group(1)}}}"), None) if m else None
        if end is None:
            out.append(lines[i])
            i += 1
            continue
        out.extend(_server(lines[i:end + 1], gained, left))
        i = end + 1
    return "\n".join(out), [GAINED[key] for key in GAINED if key in gained], left


def rewrite(path: Path, text: str) -> None:
    """The file replaced at once (never seen half written), its mode kept."""
    part = path.with_name(path.name + ".part")
    part.write_text(text)
    os.chmod(part, path.stat().st_mode & 0o777)
    os.replace(part, path)


def bring_up_to_date(config: Path, *, now: datetime | None = None) -> tuple[list[str], list[str]]:
    """A Mac's nginx config (init.py's, kept as it was written) given what it has gained, backed up beside itself
    first (.before-update-<time>): what it gained and what was left, for the log. OSError when it can't be read
    or written."""
    text = config.read_text()
    new, gained, left = updated(text)
    if new != text:
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")  # noqa: UP017 — 3.10's too
        shutil.copy2(config, config.with_name(f"{config.name}.before-update-{stamp}"))
        rewrite(config, new)
    return gained, left


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: site_update.py SITE", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        text = path.read_text()
    except FileNotFoundError:
        return 0
    new, gained, left = updated(text)
    if new != text:
        rewrite(path, new)
    for line in gained:
        print(f"  ✓ {line}")
    for line in left:
        print(f"  ⚠ {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
