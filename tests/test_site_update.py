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


# What a site without HTTPS of its own gains (a Mac's: its HTTPS is network.py's lines).
WITHOUT_HTTPS = [line for key, line in site_update.GAINED.items() if key != "http2"]


def _today() -> str:
    return nginx_conf(nginx_site(listen=(LISTEN,), server_name="_", root=WEB, upstream="http://127.0.0.1:6364"))


def test_a_config_from_before_becomes_today_s_to_the_byte_once():
    before = FIXTURE.read_text()
    assert "media-src 'self';" in before and "gzip" not in before, "the fixture is v0.1.160's"
    after, gained, left = site_update.updated(before)
    assert after == _today()
    assert gained == WITHOUT_HTTPS and left == []
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
    assert out.stdout.splitlines() == [f"  ✓ {line}" for line in WITHOUT_HTTPS]
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
    # One process: run as root (a Linux container's tests), its workers would be nobody, who can't read tmp_path.
    conf = "master_process off;\n" + conf.replace("    access_log off;\n", "    access_log off;\n" + temp, 1)
    (tmp_path / "nginx.conf").write_text(conf)
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


# What certbot --nginx --redirect makes of a site: 443 in the block, a port-80 redirect after it.
def _certbot(site: str) -> str:
    head, tail = site.replace("    listen 80;\n", "", 1).rsplit("}\n", 1)
    return (head + "    listen 443 ssl; # managed by Certbot\n    listen [::]:443 ssl ipv6only=on; # managed by Certbot\n"
            "    ssl_certificate /etc/letsencrypt/live/x/fullchain.pem; # managed by Certbot\n}\n" + tail
            + "\nserver {\n    if ($host = x) {\n        return 301 https://$host$request_uri;\n    }\n"
            "    listen 80;\n    server_name x;\n    return 404; # managed by Certbot\n}\n")


def _linux_site() -> str:
    return nginx_site(listen=("listen 80;",), server_name="x", root=Path("/opt/lumiverb/src/ui/web/dist"),
                      upstream="http://127.0.0.1:8100")


def test_https_speaks_http2_in_the_words_this_nginx_takes():
    site = _certbot(_linux_site())
    now, gained, _ = site_update.updated(site, (1, 28, 0))
    assert "    listen [::]:443 ssl ipv6only=on; # managed by Certbot\n    http2 on;\n" in now
    assert gained == [site_update.GAINED["http2"]]
    assert site_update.updated(now, (1, 28, 0))[0] == now, "once"

    before, _, _ = site_update.updated(site, (1, 24, 0))  # Ubuntu 24.04's: the listen line's parameter
    assert "listen 443 ssl http2; # managed by Certbot" in before
    assert "listen [::]:443 ssl http2 ipv6only=on; # managed by Certbot" in before
    assert "http2 on;" not in before and site_update.updated(before, (1, 24, 0))[0] == before

    assert site_update.updated(site)[0] == site, "an nginx it can't ask: left"
    assert site_update.updated(_linux_site(), (1, 28, 0))[0] == _linux_site(), "no HTTPS, nothing to do"
    redirect = site.split("\nserver {", 1)[1]
    assert "http2" not in site_update.updated(site, (1, 28, 0))[0].split("\nserver {", 1)[1] == redirect


SCRIPTS = Path(__file__).parents[1] / "scripts"
_START = "# A name none of this machine's sites serves"


def _default_site_functions(script: str) -> str:
    text = (SCRIPTS / script).read_text()
    start = text.index(_START)
    end = text.index("\n}\n", text.index("install_default_site() {")) + 3
    return text[start:end]


def test_deploy_and_update_turn_away_names_the_same_way():
    assert _default_site_functions("deploy-web.sh") == _default_site_functions("update-web.sh")


def _default_site(enabled: Path, version: str) -> str:
    script = f"{_default_site_functions('deploy-web.sh')}\ndefault_site {enabled} {version}\n"
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_the_default_site_listens_where_the_others_do(tmp_path):
    enabled = tmp_path / "sites-enabled"
    enabled.mkdir()
    (enabled / "lumiverb").write_text(_certbot(_linux_site()))
    (enabled / "lumiverb-default").write_text("server {\n    listen [::]:80 default_server;\n}\n")  # its own: not read
    text = _default_site(enabled, "1.28.0")
    listens = [line.strip() for line in text.splitlines() if line.strip().startswith("listen")]
    # certbot's block listens on [::]:443 and not [::]:80: a listener of its own there would turn away every name.
    assert listens == ["listen 80 default_server;", "listen 443 ssl default_server;",
                       "listen [::]:443 ssl default_server;"]
    assert "ssl_reject_handshake on;" in text and "return 444;" in text

    old = _default_site(enabled, "1.18.0")  # Ubuntu 22.04's: no ssl_reject_handshake, so HTTP alone
    assert "443" not in old and "ssl_reject_handshake" not in old and "listen 80 default_server;" in old


@pytest.mark.skipif(not shutil.which("nginx") or not shutil.which("openssl"), reason="no nginx here to try it on")
def test_nginx_turns_away_a_name_no_site_serves(tmp_path):
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp_path / "k.pem"),
                    "-out", str(tmp_path / "c.pem"), "-days", "1", "-subj", "/CN=x.test"],
                   capture_output=True, check=True, timeout=60)
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "index.html").write_text("<title>Lumiverb</title>")
    http, https = _free_port(), _free_port()
    version = ".".join(map(str, site_update.nginx_version()))
    enabled = tmp_path / "enabled"
    enabled.mkdir()
    # Its HTTPS speaking HTTP/2 as site_update gives it, in this nginx's words, beside a plain listen (the
    # droplet's admin tunnel): HTTP/1.1 there still.
    app = (f"server {{\n    listen 127.0.0.1:{http};\n    listen 443 ssl;\n    server_name x.test;\n"
           f"    root {tmp_path / 'www'};\n    ssl_certificate {tmp_path / 'c.pem'};\n"
           f"    ssl_certificate_key {tmp_path / 'k.pem'};\n}}\n")
    app, gained, _ = site_update.updated(app, site_update.nginx_version())
    assert gained == [site_update.GAINED["http2"]]
    (enabled / "lumiverb").write_text(app.replace("listen 443 ", f"listen 127.0.0.1:{https} "))
    default = (_default_site(enabled, version).replace("listen 80 ", f"listen 127.0.0.1:{http} ")
               .replace("listen 443 ", f"listen 127.0.0.1:{https} "))
    (enabled / "lumiverb-default").write_text(default)
    temp = "".join(f"  {d} {tmp_path / d};\n" for d in ("client_body_temp_path", "proxy_temp_path",
                                                         "fastcgi_temp_path", "uwsgi_temp_path", "scgi_temp_path"))
    (tmp_path / "nginx.conf").write_text(f"daemon off;\nmaster_process off;\npid {tmp_path}/nginx.pid;\nevents {{}}\nhttp {{\n"
                                         f"  access_log off;\n{temp}  include {enabled}/*;\n}}\n")
    nginx = ["nginx", "-e", str(tmp_path / "error.log"), "-p", str(tmp_path), "-c", str(tmp_path / "nginx.conf")]
    checked = subprocess.run([*nginx, "-t"], capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stderr
    server = subprocess.Popen(nginx, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def curl(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["curl", "-sk", "--max-time", "5", "-o", "/dev/null", "-w", "%{http_code}", *args],
                                  capture_output=True, text=True, timeout=30)

        for _ in range(100):
            if curl("-H", "Host: x.test", f"http://127.0.0.1:{http}/").stdout == "200":
                break
            time.sleep(0.05)
        else:
            pytest.fail("nginx never answered: " + (tmp_path / "error.log").read_text())
        assert curl("--resolve", f"x.test:{https}:127.0.0.1", f"https://x.test:{https}/").stdout == "200"
        version = ("-w", "%{http_version}")
        assert curl("--http2", *version, "--resolve", f"x.test:{https}:127.0.0.1",
                    f"https://x.test:{https}/").stdout.endswith("2"), "HTTPS speaks HTTP/2"
        assert curl("--http1.1", *version, "-H", "Host: x.test",
                    f"http://127.0.0.1:{http}/").stdout.endswith("1.1"), "plain HTTP still HTTP/1.1"
        for args in (("-H", "Host: elsewhere.example", f"http://127.0.0.1:{http}/"),
                     (f"http://127.0.0.1:{http}/",),
                     ("--resolve", f"elsewhere.example:{https}:127.0.0.1", f"https://elsewhere.example:{https}/"),
                     (f"https://127.0.0.1:{https}/",)):
            said = curl(*args)
            assert said.stdout == "000" and said.returncode in (35, 52, 56), f"{args} was answered"
    finally:
        server.terminate()
        server.wait(timeout=10)
