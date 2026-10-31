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
- a hidden file (/.env, /.git/) or a file name the app doesn't have answered 404, never the app's page;
- HTTP/2 where it's served over HTTPS (certbot's 443), in the words this machine's nginx takes: `http2 on;` from
  1.25.1, the listen line's `http2` before;
- nginx's version unsaid by every server block in the file, certbot's port-80 redirects too (it can't be said
  once for them all: the distribution's nginx.conf sets server_tokens itself, and a second is refused).

- on a Linux server, each line of its access log naming the site (nginx's combined format and "$host",
  `lumiverb_host`, defined in conf.d by `--log-format`), for `lumiverb-server site-visits` (site_visits.py). A Mac's
  nginx keeps no access log.

deploy-site.sh's static sites (lumiverb.io's front page, the docs) get only the last two (`--static`).

init.py and deploy-web.sh write these same lines (tests/test_site_update.py holds them to it). It runs on the
python3 of every Ubuntu deploy-web.sh supports: 3.10 at the oldest.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
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

# Its access log's lines name the site (nginx's combined format, then "$host"): a format of the http block's,
# defined in conf.d (the distribution's nginx.conf includes it there), each Lumiverb site logging with it.
LOG_FORMAT_FILE = "/etc/nginx/conf.d/lumiverb-log.conf"
LOG_FORMAT = """\
# Lumiverb's (site_update.py --log-format): its sites' access log lines name the site, for
# lumiverb-server site-visits. nginx's combined format, then "$host".
log_format lumiverb_host '$remote_addr - $remote_user [$time_local] "$request" '
                         '$status $body_bytes_sent "$http_referer" "$http_user_agent" "$host"';
"""
ACCESS_LOG = "access_log /var/log/nginx/access.log lumiverb_host;"
# ...and Lumiverb's own copy, which the service user reads (nginx's is root's and adm's): the API adds up each day of
# it (site_visits.py, upkeep). Its folder and its rotation, 14 days as nginx's own, are --log-format's to make.
SITE_LOG_DIR = "/var/log/lumiverb"
SITES_LOG = f"access_log {SITE_LOG_DIR}/sites.log lumiverb_host;"
LOG_LINES = (ACCESS_LOG, SITES_LOG)
LOGROTATE_FILE = "/etc/logrotate.d/lumiverb-sites"
LOGROTATE = f"""\
# Lumiverb's (site_update.py --log-format): its sites' access log, which the API adds up each day (site visits).
{SITE_LOG_DIR}/sites.log {{
\tdaily
\tmissingok
\trotate 14
\tcompress
\tdelaycompress
\tnotifempty
\tcreate 0640 www-data lumiverb
\tsharedscripts
\tpostrotate
\t\tinvoke-rc.d nginx rotate >/dev/null 2>&1
\tendscript
}}
"""
SERVICE_USER = "lumiverb"

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
# A listen line serving HTTPS on 443 (certbot's: `listen 443 ssl;`, `listen [::]:443 ssl ipv6only=on;`).
_TLS_LISTEN = re.compile(r"(\s*listen\s+(?:\S*:)?443\s+ssl)\b(.*)")
HTTP2_ON = (1, 25, 1)  # the http2 directive; before it, the listen line's parameter

GAINED = {
    "csp": "Video from blob: URLs plays (the CSP)",
    "referrer": "The referrer policy is the API's",
    "permissions": "The permissions policy is on",
    "delivery": "Text is compressed, and nginx doesn't say its version",
    "pages": "A hidden file or a file the app doesn't have is a 404, not the page",
    "http2": "HTTPS speaks HTTP/2",
    "tokens": "nginx doesn't say its version on any of the site's answers (certbot's redirects too)",
    "log": "Its access log names the site, nginx's and Lumiverb's own (site visits)",
}


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _http2(block: list[str], nginx: tuple[int, ...] | None, gained: set[str]) -> list[str]:
    """HTTP/2 on the block's HTTPS, if it has any and this nginx's version is known."""
    tls = [i for i, line in enumerate(block) if _TLS_LISTEN.fullmatch(line)]
    if not tls or nginx is None or any(line.strip() == "http2 on;" for line in block):
        return block
    if nginx >= HTTP2_ON:
        gained.add("http2")
        return [*block[:tls[-1] + 1], _indent(block[tls[-1]]) + "http2 on;", *block[tls[-1] + 1:]]
    out = list(block)
    for i in tls:
        m = _TLS_LISTEN.fullmatch(block[i])
        if not re.search(r"\bhttp2\b", m.group(2)):
            out[i] = f"{m.group(1)} http2{m.group(2)}"
            gained.add("http2")
    return out


def _tokens(block: list[str], gained: set[str]) -> list[str]:
    """server_tokens off in a block that doesn't say it yet, after its server_name: certbot's redirects too."""
    at = next((i for i, line in enumerate(block) if line.strip().startswith("server_name ")), None)
    if at is None or any(line.strip().startswith("server_tokens ") for line in block):
        return block
    gained.add("tokens")
    return [*block[:at + 1], _indent(block[at]) + "server_tokens off;", *block[at + 1:]]


def _logged(block: list[str], gained: set[str], left: list[str]) -> list[str]:
    """Its access logs in lumiverb_host (nginx's and Lumiverb's), right after its server_name, unless it says where
    it logs already as Lumiverb didn't write."""
    at = next((i for i, line in enumerate(block) if line.strip().startswith("server_name ")), None)
    if at is None:
        return block
    pad = _indent(block[at])
    own = [line.strip() for line in block if line.startswith(pad + "access_log ")]
    if own == list(LOG_LINES):
        return block
    if own and own != [ACCESS_LOG]:  # v0.1.164's line alone gains Lumiverb's beside it
        left.append("A block logs as it was told by hand (its access_log): its visits aren't counted")
        return block
    gained.add("log")
    rest = [line for line in block[at + 1:] if line != pad + ACCESS_LOG]
    return [*block[:at + 1], *(pad + line for line in LOG_LINES), *rest]


def _server(block: list[str], gained: set[str], left: list[str], nginx: tuple[int, ...] | None) -> list[str]:
    """One server block brought up to date. Its own directives are at its server_name's indentation; a block
    without one isn't the app's."""
    pad = next((_indent(line) for line in block if line.strip().startswith("server_name ")), None)
    if pad is None or not any(line.strip().startswith("root ") for line in block):
        return block
    block = _http2(block, nginx, gained)
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


def updated(text: str, nginx: tuple[int, ...] | None = None, *, static: bool = False,
            logged: bool = False) -> tuple[str, list[str], list[str]]:
    """The site's text with what it has gained, what it gained (for the log), and what was left as it was. HTTP/2
    only with the version of the nginx serving it (`nginx_version`); its access log named only where `logged` (a
    Linux server's: LOG_FORMAT defined); a static site of deploy-site.sh's gains only those two."""
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
        block = lines[i:end + 1]
        block = _tokens(block if static else _server(block, gained, left, nginx), gained)
        out.extend(_logged(block, gained, left) if logged else block)
        i = end + 1
    return "\n".join(out), [GAINED[key] for key in GAINED if key in gained], left


def nginx_version() -> tuple[int, ...] | None:
    """This machine's nginx's version (`nginx -v`), or None when there's none to ask."""
    try:
        said = subprocess.run(["nginx", "-v"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"nginx/(\d+)\.(\d+)\.(\d+)", said.stderr + said.stdout)
    return tuple(int(part) for part in m.groups()) if m else None


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


def _write(path: Path, text: str) -> bool:
    """Lumiverb's own file, whole; True if it changed."""
    try:
        if path.read_text() == text:
            return False
    except FileNotFoundError:
        pass
    part = path.with_name(path.name + ".part")
    part.write_text(text)
    os.chmod(part, 0o644)
    os.replace(part, path)
    return True


def write_log_format(path: Path, *, log_dir: Path = Path(SITE_LOG_DIR),
                     logrotate: Path | None = Path(LOGROTATE_FILE)) -> bool:
    """What its sites' access logs need, before a site uses them: LOG_FORMAT in conf.d, Lumiverb's log folder
    (root's, the service user's group may read it: 0750) and its rotation; True if any changed."""
    changed = _write(path, LOG_FORMAT)
    if not log_dir.is_dir():
        log_dir.mkdir(mode=0o750, parents=True)
        changed = True
    try:
        import grp

        group = grp.getgrnam(SERVICE_USER).gr_gid
    except (ImportError, KeyError):  # no service user here (a test's machine): the folder stays its maker's
        group = None
    if group is not None and log_dir.stat().st_gid != group:
        os.chown(log_dir, 0, group)
        os.chmod(log_dir, 0o750)
        changed = True
    if logrotate is not None and logrotate.parent.is_dir():
        changed = _write(logrotate, LOGROTATE) or changed
    return changed


def main(argv: list[str]) -> int:
    if argv[1:2] == ["--log-format"] and len(argv) == 3:
        # Tests name their own folders: a server's are /var/log/lumiverb and /etc/logrotate.d.
        log_dir = Path(os.environ.get("LUMIVERB_SITE_LOG_DIR", SITE_LOG_DIR))
        logrotate = Path(os.environ.get("LUMIVERB_LOGROTATE_D", str(Path(LOGROTATE_FILE).parent))) / "lumiverb-sites"
        if write_log_format(Path(argv[2]), log_dir=log_dir, logrotate=logrotate):
            print(f"  ✓ Its sites' access log lines name the site (lumiverb_host), kept in {log_dir} too")
        return 0
    static = argv[1:2] == ["--static"]
    if len(argv) != 2 + static:
        print("Usage: site_update.py [--static] SITE | --log-format FILE", file=sys.stderr)
        return 2
    path = Path(argv[-1])
    try:
        text = path.read_text()
    except FileNotFoundError:
        return 0
    new, gained, left = updated(text, None if static else nginx_version(), static=static, logged=True)
    if new != text:
        rewrite(path, new)
    for line in gained:
        print(f"  ✓ {line}")
    for line in left:
        print(f"  ⚠ {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
