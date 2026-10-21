"""scripts/deploy-site.sh: lumiverb.io's page (site/) served by the demo
server's nginx (docs/demo-server.md). The site it writes for nginx is read
from --dry-run and tried on a real nginx; the steps around it run with
nginx, systemctl and curl stubbed; its DNS check and HSTS are deploy-web.sh's,
word for word."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "deploy-site.sh"
DEPLOY_WEB = REPO / "scripts" / "deploy-web.sh"


def _run(
    *args: str, app_dir: Path = REPO, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "LUMIVERB_APP_DIR": str(app_dir), **(env or {})},
    )


def _site(*names: str) -> str:
    out = _run(*[a for n in names for a in ("--domain", n)], "--dry-run")
    assert out.returncode == 0, out.stderr
    return out.stdout


def _directives(policy: str) -> set[str]:
    return {d.strip() for d in policy.split(";") if d.strip()}


def test_it_serves_the_checkout_s_site_folder_and_nothing_around_it():
    site = _site("lumiverb.io", "www.lumiverb.io")
    assert "server_name lumiverb.io www.lumiverb.io;" in site
    assert f"root {REPO}/site;" in site, "site/, never the checkout with its .git"
    assert "try_files $uri $uri/ =404;" in site, "a missing file is a 404, not the page"


def test_its_policy_is_the_page_s_own():
    page = (REPO / "site" / "index.html").read_text(encoding="utf-8")
    [meta] = re.findall(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page)
    [header] = re.findall(r'add_header Content-Security-Policy "([^"]+)"', _site("lumiverb.io"))
    # frame-ancestors is a header's alone: a page's own policy can't say it.
    assert _directives(header) == _directives(meta) | {"frame-ancestors 'none'"}


@pytest.mark.parametrize(
    "name",
    [
        "lumiverb.io; root /",
        "lumiverb.io\n",
        "-lumiverb.io",
        "localhost",
        "lumi verb.io",
        "lumiverb.io.",
    ],
)
def test_a_name_goes_into_nginx_only_as_a_name(name):
    out = _run("--domain", name, "--dry-run")
    assert out.returncode == 1 and f"Not a domain name: {name}" in out.stderr, out.stderr


def test_it_needs_a_name_and_the_site():
    out = _run("--dry-run")
    assert out.returncode == 1 and "Required: --domain" in out.stderr
    out = _run("--domain", "app.example.com", "--dry-run")
    assert out.returncode == 1 and "Replace example.com" in out.stderr


def test_without_the_site_in_the_checkout_it_stops(tmp_path):
    out = _run("--domain", "lumiverb.io", "--dry-run", app_dir=tmp_path)
    assert out.returncode == 1 and f"No site in {tmp_path}/site" in out.stderr, out.stderr


def _stubbed(
    tmp_path: Path, *, nginx_ok: bool
) -> tuple[subprocess.CompletedProcess, Path, Path, Path]:
    """The script as root, its nginx -t answering as told, systemctl and curl stubbed: what it does around nginx."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    called = tmp_path / "called"
    stubs = {
        "id": "echo 0\n",
        "nginx": f'echo "nginx $*" >> {called}\nexit {0 if nginx_ok else 1}\n',
        "systemctl": f'echo "systemctl $*" >> {called}\n',
        "curl": "exit 7\n",
    }
    for name, body in stubs.items():
        (bin_ / name).write_text("#!/usr/bin/env bash\n" + body)
        (bin_ / name).chmod(0o755)
    conf, link = tmp_path / "available" / "lumiverb-site", tmp_path / "enabled" / "lumiverb-site"
    conf.parent.mkdir()
    link.parent.mkdir()
    out = _run(
        "--domain",
        "lumiverb.io",
        "--skip-certbot",
        env={
            "PATH": f"{bin_}:{os.environ['PATH']}",
            "LUMIVERB_SITE_CONF": str(conf),
            "LUMIVERB_SITE_LINK": str(link),
        },
    )
    return out, conf, link, called


def test_it_writes_the_site_and_reloads_nginx(tmp_path):
    out, conf, link, called = _stubbed(tmp_path, nginx_ok=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert conf.read_text() == _site("lumiverb.io")
    assert link.is_symlink() and link.resolve() == conf.resolve()
    assert called.read_text().splitlines() == ["nginx -t", "systemctl reload nginx"]
    assert "Skipping certbot" in out.stdout
    assert "didn't answer for lumiverb.io" in out.stdout, "said when the page doesn't answer"


def test_a_site_nginx_refuses_goes_again_at_once(tmp_path):
    # Left enabled, it would stop every later reload, the web app's included.
    out, conf, link, called = _stubbed(tmp_path, nginx_ok=False)
    assert out.returncode == 1 and "nginx refused the site" in out.stderr, out.stderr
    assert not link.exists() and not link.is_symlink()
    assert "systemctl" not in called.read_text(), "nothing reloaded"


def _function(text: str, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"no {name}()"
    return m.group(0)


def test_its_dns_check_and_hsts_are_deploy_web_s():
    web, site = DEPLOY_WEB.read_text(), SCRIPT.read_text()
    for name in ("private_v4", "domain_points_here", "add_hsts"):
        assert _function(site, name) == _function(web, name), (
            f"{name}() differs from deploy-web.sh's"
        )
    assert site.index('if domain_points_here "$d"') < site.index('certbot "${certbot_args[@]}"'), (
        "each name is checked before Let's Encrypt is asked"
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not shutil.which("nginx"), reason="no nginx here to try the site on")
def test_nginx_takes_the_site_and_serves_the_page_alone(tmp_path):
    port = _free_port()
    site = _site("lumiverb.io").replace("listen 80;", f"listen 127.0.0.1:{port};")
    (tmp_path / "site.conf").write_text(site)
    # nginx's own files in this test's folder: run as anyone, it writes nowhere else.
    temp_paths = (
        "client_body_temp_path",
        "proxy_temp_path",
        "fastcgi_temp_path",
        "uwsgi_temp_path",
        "scgi_temp_path",
    )
    (tmp_path / "nginx.conf").write_text(
        f"master_process off;\npid {tmp_path}/nginx.pid;\nerror_log {tmp_path}/error.log;\nevents {{}}\n"
        "http {\n"
        "  types { text/html html; text/css css; application/javascript js; image/webp webp;"
        " image/svg+xml svg; image/jpeg jpg; }\n"
        "  access_log off;\n"
        + "".join(f"  {d} {tmp_path / d};\n" for d in temp_paths)
        + f"  include {tmp_path}/site.conf;\n}}\n"
    )
    nginx = [
        "nginx",
        "-e",
        str(tmp_path / "error.log"),
        "-p",
        str(tmp_path),
        "-c",
        str(tmp_path / "nginx.conf"),
    ]
    checked = subprocess.run([*nginx, "-t"], capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stderr
    server = subprocess.Popen(
        [*nginx, "-g", "daemon off;"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:

        def get(path: str) -> tuple[int, dict[str, str], bytes]:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", headers={"Host": "lumiverb.io"}
            )
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
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        status, headers, _ = get("/images/search.webp")
        assert (status, headers["Content-Type"]) == (200, "image/webp")
        assert get("/no-such-page")[0] == 404, "a missing file is a 404, not the page"
        for outside in (
            "/../README.md",
            "/%2e%2e/README.md",
            "/.git/HEAD",
            "/../scripts/deploy-site.sh",
        ):
            status, _, body = get(outside)
            assert status in (400, 404) and b"Lumiverb" not in body, (
                f"{outside} reached outside site/"
            )
    finally:
        server.terminate()
        server.wait(10)
