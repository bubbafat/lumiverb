"""What the web app's nginx site has gained, given to one written before (src/server/lifecycle/site_update.py): a
Mac's config as v0.1.160 wrote it becomes today's to the byte, once, and a real nginx serves today's as it should:
the CSP lets blob: video through, the API's referrer and permissions policies, text compressed, nginx's version
unsaid, and a hidden file or a file name the app doesn't have a 404, never the page. A Linux site's update is
tests/test_deploy_scripts.py's (update-web.sh runs this same file)."""

from __future__ import annotations

import gzip
import http.server
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.server.lifecycle import site_update
from src.server.lifecycle.init import nginx_conf, nginx_site

pytestmark = pytest.mark.fast

FIXTURE = Path(__file__).parent / "fixtures" / "nginx-mac-v0.1.160.conf"
LISTEN = "include /usr/local/var/lumiverb/nginx/listen.conf;"
WEB = Path("/opt/homebrew/opt/lumiverb/libexec/src/ui/web/dist")


def _today() -> str:
    return nginx_conf(nginx_site(listen=(LISTEN,), server_name="_", root=WEB, upstream="http://127.0.0.1:6364"))


def test_a_config_from_before_becomes_today_s_to_the_byte_once():
    before = FIXTURE.read_text()
    assert "media-src 'self';" in before and "gzip" not in before, "the fixture is v0.1.160's"
    after, gained, left = site_update.updated(before)
    assert after == _today()
    assert gained == list(site_update.GAINED.values()) and left == []
    assert site_update.updated(after) == (after, [], []), "up to date: nothing more"


def test_what_was_changed_by_hand_is_left_and_said():
    before = FIXTURE.read_text().replace("            try_files $uri $uri/ /index.html;\n",
                                         "            try_files $uri /index.html;\n")
    after, _, left = site_update.updated(before)
    assert "try_files $uri /index.html;" in after and "location ~ /\\." not in after
    assert left == ["Its location / isn't as Lumiverb wrote it: a hidden file or a file the app doesn't have "
                    "still gets the page"]
    assert "gzip on;" in after, "the rest it still gains"


def test_a_block_that_isnt_the_app_s_is_left_alone():
    # certbot's redirect block (it has its own server_name) has none of the app's lines.
    redirect = ("server {\n    if ($host = x) {\n        return 301 https://$host$request_uri;\n    }\n"
                "    listen 80;\n    server_name x;\n    return 404;\n}\n")
    assert site_update.updated(redirect) == (redirect, [], [])


def test_a_mac_s_config_is_backed_up_beside_itself_first(tmp_path):
    config = tmp_path / "nginx.conf"
    config.write_text(FIXTURE.read_text())
    config.chmod(0o640)
    gained, left = site_update.bring_up_to_date(config, now=datetime(2026, 10, 30, 21, 5, tzinfo=UTC))
    assert gained and not left
    assert config.read_text() == _today() and config.stat().st_mode & 0o777 == 0o640
    assert (tmp_path / "nginx.conf.before-update-20261030T210500Z").read_text() == FIXTURE.read_text()
    assert site_update.bring_up_to_date(config) == ([], [])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["nginx.conf", "nginx.conf.before-update-20261030T210500Z"]


def test_the_script_is_the_standard_library_s_alone(tmp_path):
    # update-web.sh runs it as `python3 -I site_update.py SITE`, outside Lumiverb's environment.
    site = tmp_path / "lumiverb"
    site.write_text(FIXTURE.read_text())
    script = Path(site_update.__file__)
    out = subprocess.run([sys.executable, "-I", "-S", str(script), str(site)], capture_output=True, text=True,
                         timeout=30, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert site.read_text() == _today()
    assert out.stdout.splitlines() == [f"  ✓ {line}" for line in site_update.GAINED.values()]
    missing = subprocess.run([sys.executable, "-I", "-S", str(script), str(tmp_path / "none")], capture_output=True,
                             text=True, timeout=30)
    assert (missing.returncode, missing.stdout) == (0, ""), "no site, nothing to do"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Api(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — http.server's name
        body = b'{"path": "%s", "items": [%s]}' % (self.path.encode(), b",".join([b'"a clip"'] * 200))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.mark.skipif(not shutil.which("nginx"), reason="no nginx here to try the config on")
def test_nginx_serves_today_s_site_as_it_should(tmp_path):
    web = tmp_path / "dist"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>Lumiverb</title>")
    (web / "assets" / "index-abc123.js").write_text("console.log('the app');\n" * 200)
    (web / "vite.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    port = _free_port()
    api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    (tmp_path / "listen.conf").write_text(f"listen 127.0.0.1:{port};\n")
    conf = nginx_conf(nginx_site(listen=(f"include {tmp_path / 'listen.conf'};",), server_name="_", root=web,
                                 upstream=f"http://127.0.0.1:{api.server_address[1]}"))
    # nginx's own files in this test's folder: run as anyone, it writes nowhere else.
    temp = "".join(f"    {d} {tmp_path / d};\n" for d in ("client_body_temp_path", "proxy_temp_path",
                                                             "fastcgi_temp_path", "uwsgi_temp_path", "scgi_temp_path"))
    (tmp_path / "nginx.conf").write_text(conf.replace("    access_log off;\n", "    access_log off;\n" + temp, 1))
    nginx = ["nginx", "-e", str(tmp_path / "error.log"), "-p", str(tmp_path), "-c", str(tmp_path / "nginx.conf")]
    checked = subprocess.run([*nginx, "-t"], capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stderr
    server = subprocess.Popen(nginx, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def get(path: str, **headers: str) -> tuple[int, dict[str, str], bytes]:
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), e.read()

        for _ in range(100):
            try:
                status, headers, body = get("/")
                break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("nginx never answered: " + (tmp_path / "error.log").read_text())

        assert status == 200 and b"<title>Lumiverb" in body
        assert "media-src 'self' blob:;" in headers["Content-Security-Policy"]
        assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
        assert headers["Server"] == "nginx", "no version"

        status, headers, body = get("/assets/index-abc123.js", **{"Accept-Encoding": "gzip"})
        assert status == 200 and headers["Content-Encoding"] == "gzip"
        assert gzip.decompress(body).startswith(b"console.log")
        assert headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert headers["Permissions-Policy"], "a location's headers are the server's too"

        status, headers, body = get("/v1/assets/a1/thumbnail.jpg", **{"Accept-Encoding": "gzip"})
        assert status == 200 and headers["Content-Encoding"] == "gzip", "the API's answers, a name with a dot too"
        assert b'"path": "/v1/assets/a1/thumbnail.jpg"' in gzip.decompress(body)

        for page in ("/libraries/lib_1/browse", "/people/p1", "/no-such-page"):
            status, _, body = get(page)
            assert status == 200 and b"<title>Lumiverb" in body, f"{page} is the app's to route"
        assert get("/vite.svg")[0] == 200
        for probe in ("/.env", "/.git/config", "/.aws/credentials", "/wp-login.php", "/robots.txt",
                      "/favicon.ico", "/backup.zip", "/libraries/.env"):
            status, headers, body = get(probe)
            assert status == 404 and b"Lumiverb" not in body, f"{probe} got the page"
            assert headers["X-Frame-Options"] == "DENY", "a 404 has the headers too"
    finally:
        server.terminate()
        server.wait(timeout=10)
        api.shutdown()
