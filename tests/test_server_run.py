"""lumiverb-server run (src/server/lifecycle/cli.py): a Mac's service. The
children it runs match a Linux install's systemd units; migrations wait for
Postgres; and end to end, on real Postgres, it migrates, serves the API,
runs the scheduler, and stops both cleanly on SIGTERM."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import create_engine, make_url, text
from testcontainers.postgres import PostgresContainer

from src.server.lifecycle import cli, network
from src.server.lifecycle.init import nginx_conf, nginx_site
from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_server_postgres import NO_POSTGRES, free_port, postgres_bin

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.fast
def test_the_children_are_linux_s_units(tmp_path):
    env = {"DATA_DIR": str(tmp_path), "API_PORT": "8100", "API_LISTEN_HOST": "0.0.0.0", "ADMIN_KEY": "k"}
    api, scheduler = cli.server_children(env, python="/py", cwd=tmp_path)

    assert api.argv == ["/py", "-m", "uvicorn", "src.server.api.main:app", "--no-access-log",
                        "--host", "0.0.0.0", "--port", "8100", "--workers", "2"]
    assert api.env["TMPDIR"] == str(tmp_path / "tmp") and api.empty_first == tmp_path / "tmp"
    assert (api.restart_after, api.stop_within, api.term_main_only) == (5, 90, False)

    assert scheduler.argv == ["/py", "-m", "src.server.scheduler"]
    assert scheduler.env["TMPDIR"] == str(tmp_path / "worker-tmp") and scheduler.empty_first == tmp_path / "worker-tmp"
    assert scheduler.env["XDG_CACHE_HOME"] == str(tmp_path / "cache")
    assert (scheduler.restart_after, scheduler.stop_within, scheduler.term_main_only) == (30, 40, True)
    assert api.env["PYTHONUNBUFFERED"] == scheduler.env["PYTHONUNBUFFERED"] == "1"
    assert api.env["ADMIN_KEY"] == "k" and "ADMIN_KEY" not in scheduler.env  # each part only its secrets
    assert "LUMIVERB_LOG_FILE" not in api.env, "not launchd's: started from a terminal"


@pytest.mark.fast
def test_the_api_is_told_where_launchds_log_is(tmp_path):
    # Admin › Logs reads it (src/server/service_log.py); the scheduler needn't.
    log_file = tmp_path / "lumiverb.log"
    api, scheduler = cli.server_children({"DATA_DIR": str(tmp_path)}, python="/py", cwd=tmp_path, log_file=log_file)
    assert api.env["LUMIVERB_LOG_FILE"] == str(log_file)
    assert "LUMIVERB_LOG_FILE" not in scheduler.env


@pytest.mark.fast
def test_what_the_env_file_names_runs_first_and_the_api_waits_for_migrations(tmp_path):
    migrated = threading.Event()
    env = {"DATA_DIR": str(tmp_path), "CONTROL_PLANE_DATABASE_URL": "postgresql+psycopg2://lumiverb:pw@127.0.0.1:6468/cp",
           "PG_BIN": "/pg/bin", "NGINX_BIN": "/nginx/nginx", "NGINX_CONFIG": "/etc/nginx.conf",
           # An older install's env file names Quickwit: never started (ADR-019: search is in PostgreSQL).
           "QUICKWIT_BIN": "/qw/quickwit", "QUICKWIT_CONFIG": "/etc/quickwit.yaml"}
    children = cli.server_children(env, migrated=migrated, python="/py", cwd=tmp_path)
    # Stopped in reverse: the scheduler first, PostgreSQL (its clients gone) last.
    assert [c.name for c in children] == ["postgres", "nginx", "api", "scheduler"]
    postgres, nginx, api, scheduler = children
    assert postgres.argv == ["/pg/bin/postgres", "-D", str(tmp_path / "postgres"), "-p", "6468",
                             "-c", "listen_addresses=127.0.0.1", "-c", "unix_socket_directories="]
    assert postgres.term_main_only, "the postmaster stops its own backends"
    assert postgres.stop_signal == signal.SIGINT, "a fast shutdown: its clients have gone"
    assert postgres.env["LC_ALL"] == "C", "macOS's postmaster won't start without a locale"
    assert nginx.argv == ["/nginx/nginx", "-p", f"{tmp_path / 'nginx'}/", "-c", "/etc/nginx.conf", "-e", "stderr"]
    assert all(c.start_after is None for c in (postgres, nginx))
    assert api.start_after is migrated and scheduler.start_after is migrated


@pytest.mark.fast
def test_each_part_gets_only_the_secrets_it_needs(tmp_path):
    secrets = {"CONTROL_PLANE_DATABASE_URL": "postgresql+psycopg2://lumiverb:pw@127.0.0.1:6468/cp",
               "TENANT_DATABASE_URL_TEMPLATE": "postgresql+psycopg2://lumiverb:pw@127.0.0.1:6468/{tenant_id}",
               "ADMIN_KEY": "a", "API_SECRET_KEY": "b", "JWT_SECRET": "c", "SMTP_PASSWORD": "d"}
    env = {"DATA_DIR": str(tmp_path), "PG_BIN": "/pg/bin", "NGINX_BIN": "/n", "NGINX_CONFIG": "/c",
           "HOME": "/Users/x", "PATH": "/usr/bin", **secrets}
    postgres, nginx, api, scheduler = cli.server_children(env, python="/py", cwd=tmp_path)
    for child in (postgres, nginx):
        assert not set(secrets) & set(child.env), child.name
        assert (child.env["HOME"], child.env["PATH"]) == ("/Users/x", "/usr/bin")
    # It reads the database, no more (the template: the server's settings require it, the same credentials).
    assert set(secrets) & set(scheduler.env) == {"CONTROL_PLANE_DATABASE_URL", "TENANT_DATABASE_URL_TEMPLATE"}
    assert set(secrets) <= set(api.env)  # it signs people in, checks keys, sends mail
    assert postgres.argv[postgres.argv.index("-p") + 1] == "6468"  # its port, from the URL it isn't given


@pytest.mark.fast
def test_the_api_and_the_scheduler_can_read_their_settings_from_what_they_are_given(tmp_path):
    # In a folder with no .env (an install's), each child's environment alone
    # must make the server's settings: the scheduler stopped at start when a
    # required one was held back, and a checkout's .env hid it.
    env = {"DATA_DIR": str(tmp_path), "HOME": str(tmp_path), "PATH": os.environ["PATH"],
           "CONTROL_PLANE_DATABASE_URL": "postgresql+psycopg2://lumiverb:pw@127.0.0.1:6468/control_plane",
           "TENANT_DATABASE_URL_TEMPLATE": "postgresql+psycopg2://lumiverb:pw@127.0.0.1:6468/{tenant_id}",
           "ADMIN_KEY": "a", "API_SECRET_KEY": "b", "JWT_SECRET": "c"}
    code = "from src.server.config import Settings; Settings(); print('read')"
    for child in cli.server_children(env, python=sys.executable, cwd=tmp_path):
        done = subprocess.run([sys.executable, "-c", code], env={**child.env, "PYTHONPATH": str(REPO)}, cwd=tmp_path,
                              capture_output=True, text=True)
        assert done.stdout.strip() == "read", f"{child.name}: {done.stderr[-400:]}"


@pytest.mark.fast
def test_what_quickwit_left_goes_at_start_and_only_that(tmp_path, caplog):
    """An install from before search moved into PostgreSQL (ADR-019): its
    index and the config init wrote for it go; the env file's lines stay
    (the admin's), and a second start finds nothing to do."""
    data, etc = tmp_path / "var", tmp_path / "etc"
    (data / "quickwit" / "indexes").mkdir(parents=True)
    (data / "quickwit" / "indexes" / "split").write_bytes(b"x" * 10)
    (data / "previews").mkdir()
    etc.mkdir()
    config = etc / "quickwit.yaml"
    config.write_text("version: 0.8\nnode_id: lumiverb\nlisten_address: 127.0.0.1\n")
    env = {"DATA_DIR": str(data), "QUICKWIT_BIN": "/opt/homebrew/opt/quickwit/bin/quickwit",
           "QUICKWIT_CONFIG": str(config), "QUICKWIT_URL": "http://127.0.0.1:6466"}

    with caplog.at_level(logging.INFO, logger=cli.logger.name):
        assert cli.forget_quickwit(env, data) == [data / "quickwit", config]
    assert not (data / "quickwit").exists() and not config.exists()
    assert (data / "previews").is_dir(), "nothing else in DATA_DIR"
    assert "Quickwit isn't used any more" in caplog.text and "QUICKWIT_* lines" in caplog.text
    assert cli.forget_quickwit(env, data) == []  # the next start: nothing to do


@pytest.mark.fast
def test_a_quickwit_config_init_didnt_write_is_left_alone(tmp_path):
    theirs = tmp_path / "my-quickwit.yaml"
    theirs.write_text("version: 0.8\nnode_id: someone-elses\n")
    assert cli.forget_quickwit({"QUICKWIT_CONFIG": str(theirs)}, tmp_path / "var") == []
    assert theirs.exists()


# The formula's service `stop_timeout` (launchd's ExitTimeOut) in
# bubbafat/homebrew-lumiverb: past it launchd kills the whole service.
HOMEBREW_STOP_TIMEOUT_SEC = 240


@pytest.mark.fast
def test_stopping_everything_fits_in_the_formula_s_stop_timeout(tmp_path):
    env = {"DATA_DIR": str(tmp_path), "CONTROL_PLANE_DATABASE_URL": "postgresql+psycopg2://l:p@127.0.0.1:6468/c",
           "PG_BIN": "/pg", "NGINX_BIN": "/n", "NGINX_CONFIG": "/c"}
    # One after another, each at its longest.
    assert sum(c.stop_within for c in cli.server_children(env, python="/py", cwd=tmp_path)) <= HOMEBREW_STOP_TIMEOUT_SEC


@pytest.mark.fast
def test_which_folders_the_scheduler_reads_is_the_env_file_s_never_assumed(tmp_path):
    *_, scheduler = cli.server_children({"DATA_DIR": str(tmp_path)}, python="/py", cwd=tmp_path)
    assert "LUMIVERB_ROOT_MAP" not in scheduler.env, "no map: it reads nothing"
    local = '{"/": "/"}'
    *_, scheduler = cli.server_children({"DATA_DIR": str(tmp_path), "LUMIVERB_ROOT_MAP": local},
                                        python="/py", cwd=tmp_path)
    assert scheduler.env["LUMIVERB_ROOT_MAP"] == local


@pytest.mark.fast
def test_the_scheduler_never_reads_the_install_s_settings(tmp_path):
    env = {"DATA_DIR": str(tmp_path), "LUMIVERB_REFUSED_ROOTS": '["/elsewhere"]'}
    *_, scheduler = cli.server_children(env, python="/py", cwd=tmp_path, config_dir=Path("/opt/homebrew/etc/lumiverb"))
    assert json.loads(scheduler.env["LUMIVERB_REFUSED_ROOTS"]) == ["/elsewhere", "/opt/homebrew/etc/lumiverb"]


@pytest.mark.fast
def test_refused_folders_it_can_t_read_stop_it_before_anything_starts(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "Supervisor", lambda *a, **k: pytest.fail("nothing may start"))
    monkeypatch.setattr(cli, "make_cluster_if_missing", lambda *a, **k: pytest.fail("nothing may be made"))
    env_file = tmp_path / "env"
    env_file.write_text(f"CONTROL_PLANE_DATABASE_URL=postgresql://x\nDATA_DIR={tmp_path}\nPG_BIN=/pg\n"
                        "LUMIVERB_REFUSED_ROOTS=/Volumes/private\n")
    assert cli.main(["run", "--env", str(env_file)]) == 1
    assert capsys.readouterr().err.strip().startswith("✗ LUMIVERB_REFUSED_ROOTS must be a JSON list of folders")


@pytest.mark.fast
def test_nothing_named_is_the_api_and_the_scheduler_only(tmp_path):
    assert [c.name for c in cli.server_children({"DATA_DIR": str(tmp_path)}, python="/py", cwd=tmp_path)] == [
        "api", "scheduler"]


@pytest.mark.fast
def test_api_defaults_and_workers_setting(tmp_path):
    api, _ = cli.server_children({"DATA_DIR": str(tmp_path)}, python="/py", cwd=tmp_path)
    assert api.argv[-6:] == ["--host", "127.0.0.1", "--port", "8000", "--workers", "2"]
    api, _ = cli.server_children({"DATA_DIR": str(tmp_path), "LUMIVERB_API_WORKERS": "4"}, python="/py", cwd=tmp_path)
    assert api.argv[-1] == "4"


@pytest.mark.fast
def test_migrations_wait_for_postgres():
    calls: list[str] = []

    def flaky(url):
        calls.append(url)
        if len(calls) < 3:
            raise OSError("connection refused")
        return 0

    assert cli.migrate_when_ready("db", threading.Event(), migrate_fn=flaky, first_wait=0.01) is True
    assert len(calls) == 3


@pytest.mark.fast
def test_a_stop_while_waiting_for_postgres_ends_it():
    stop = threading.Event()

    def never(url):
        stop.set()
        raise OSError("connection refused")

    assert cli.migrate_when_ready("db", stop, migrate_fn=never, first_wait=0.01) is False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


@pytest.mark.slow
def test_run_migrates_serves_and_stops_cleanly(tmp_path):
    lumiverb_server = Path(sys.executable).parent / "lumiverb-server"  # the console script a formula installs
    assert lumiverb_server.exists()
    with PostgresContainer(PG_IMAGE) as pg:
        # A control database that isn't there yet: run makes it (no createdb on a Mac).
        server_url = make_url(_ensure_psycopg2(pg.get_connection_url()))
        control_url = server_url.set(database=f"made_by_run_{server_url.database}").render_as_string(
            hide_password=False)
        engine = create_engine(control_url)
        port = _free_port()
        env_file = tmp_path / "env"
        env_file.write_text("\n".join([
            f"CONTROL_PLANE_DATABASE_URL={control_url}",
            f"TENANT_DATABASE_URL_TEMPLATE={control_url.rsplit('/', 1)[0]}/{{tenant_id}}",
            f"DATA_DIR={tmp_path / 'data'}",
            f"API_PORT={port}",
            "API_LISTEN_HOST=127.0.0.1",
            "LUMIVERB_API_WORKERS=1",
            "ADMIN_KEY=test-admin-run",
            "JWT_SECRET=test-jwt-run",
        ]))
        out = tmp_path / "service.log"
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("CONTROL_PLANE", "TENANT_DATABASE", "LUMIVERB_", "API_", "DATA_DIR"))}
        with out.open("w") as log:
            service = subprocess.Popen([str(lumiverb_server), "--env", str(env_file), "run"],
                                       stdout=log, stderr=subprocess.STDOUT, env=clean_env)
        try:
            deadline = time.monotonic() + 120
            while not _healthy(port):
                assert service.poll() is None, out.read_text()
                assert time.monotonic() < deadline, out.read_text()
                time.sleep(0.5)
            with engine.connect() as conn:
                assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            # In the log while it runs, not held in a buffer until it stops.
            running = out.read_text()
            assert "lumiverb-server: Created the control database" in running, running
            assert "lumiverb-server: Control plane" in running, running
        finally:
            service.send_signal(signal.SIGTERM)
            try:
                code = service.wait(timeout=120)
            except subprocess.TimeoutExpired:
                service.kill()
                raise
            engine.dispose()

    said = out.read_text()
    assert code == 0, said
    assert "api started" in said and "scheduler started" in said, said
    assert said.index("scheduler stopped") < said.index("api stopped"), said


def _nginx_bin() -> Path | None:
    for candidate in ("/opt/homebrew/opt/nginx/bin/nginx", "/usr/local/opt/nginx/bin/nginx", "/usr/sbin/nginx"):
        if Path(candidate).exists():
            return Path(candidate)
    return None


def _setup_needed(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/v1/setup", timeout=5) as response:
            return response.status == 200 and b'"needed":true' in response.read()
    except OSError:
        return False


def _over_https(base: str, ca: Path) -> bool:
    """/v1/setup over HTTPS, the certificate checked against Lumiverb's
    authority for the URL's host (as a device that trusts it would)."""
    import ssl

    context = ssl.create_default_context(cafile=str(ca))
    context.check_hostname = True
    try:
        with urllib.request.urlopen(f"{base}/v1/setup", timeout=5, context=context) as response:
            return response.status == 200 and b'"needed":true' in response.read()
    except OSError:
        return False


def _first_answer(url: str) -> tuple[int, str | None]:
    """The status and Location of a URL's answer, not following a redirect."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        with urllib.request.build_opener(NoRedirect).open(url, timeout=5) as response:
            return response.status, response.headers.get("Location")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location")


def _chosen(data: Path, chosen: network.Choice, out: Path, ready: Callable[[], bool],
            defaults: network.Ports, ports: network.Ports | None = None) -> None:
    """Settings › Network's choice, as the API writes it: run rewrites nginx's
    lines and restarts it."""
    network.set_choice(data, chosen)
    if ports is not None:
        network.set_ports(data, ports)
    at = network.ports(data, defaults)
    deadline = time.monotonic() + 30
    while f"Network: {network.said_with_ports(chosen, at)}; restarting nginx" not in out.read_text() or not ready():
        assert time.monotonic() < deadline, out.read_text()
        time.sleep(0.5)


def _other_devices_may_use_it(data: Path, web_port: int, tls_port: int, out: Path) -> None:
    """Other devices reach it over plain HTTP once an admin says so; HTTPS
    beside it once they add it, with Lumiverb's authority's certificate for
    this machine's names (on the network's address too, where this machine
    has one), a device using it noted; then HTTPS alone, plain HTTP from the
    network sent there. This machine's own plain HTTP stays throughout."""
    lan = network.lan_address()
    if lan:
        assert not _setup_needed(f"http://{lan}:{web_port}"), "this machine only, until an admin says"
    defaults = network.Ports(web_port, tls_port)
    _chosen(data, network.Choice(other_devices=True), out,
            lambda: _setup_needed(f"http://{lan or '127.0.0.1'}:{web_port}"), defaults)
    assert not (data / "tls").exists(), "no certificate until HTTPS is chosen"

    ca = data / "tls" / "ca.crt"
    _chosen(data, network.Choice(other_devices=True, https=True), out,
            lambda: _over_https(f"https://127.0.0.1:{tls_port}", ca), defaults)
    with urllib.request.urlopen(f"http://127.0.0.1:{web_port}/lumiverb-ca.crt", timeout=5) as response:
        assert response.read() == ca.read_bytes(), "the authority's certificate, for a device to trust"
        assert response.headers["Content-Type"] == "application/x-x509-ca-cert"
    if lan:
        assert _setup_needed(f"http://{lan}:{web_port}"), "plain HTTP stays beside HTTPS"
        assert network.https_seen(data) is None
        assert _over_https(f"https://{lan}:{tls_port}", ca)
        assert network.https_seen(data).address == lan, "a device using HTTPS, as nginx passed it on"
        with urllib.request.urlopen(f"http://{lan}:{web_port}/lumiverb-ca.crt", timeout=5) as response:
            assert response.read() == ca.read_bytes(), "a device fetches it before it can trust HTTPS"

    _chosen(data, network.Choice(other_devices=True, https=True, plain_http=False), out,
            lambda: _over_https(f"https://127.0.0.1:{tls_port}", ca), defaults)
    assert _setup_needed(f"http://127.0.0.1:{web_port}"), "this machine keeps plain HTTP"
    if lan:
        assert _over_https(f"https://{lan}:{tls_port}", ca)
        assert _first_answer(f"http://{lan}:{web_port}/v1/setup") == (301, f"https://{lan}:{tls_port}/v1/setup")
        with urllib.request.urlopen(f"http://{lan}:{web_port}/lumiverb-ca.crt", timeout=5) as response:
            assert response.read() == ca.read_bytes(), "still: a new device needs it first"
    lines = network.listen_file(data).read_text()
    assert f"listen {tls_port} ssl default_server;" in lines and "ssl_protocols TLSv1.2 TLSv1.3;" in lines
    assert "nginx restarting" in out.read_text()

    # Ports an admin chose (Settings › Network): the web and the API on each, this machine's own too.
    http, https = free_port(), free_port()
    _chosen(data, network.Choice(other_devices=True, https=True), out,
            lambda: _setup_needed(f"http://127.0.0.1:{http}") and _over_https(f"https://127.0.0.1:{https}", ca),
            defaults, network.Ports(http, https))
    assert not _setup_needed(f"http://127.0.0.1:{web_port}"), "the old port is let go"
    if lan:
        assert _setup_needed(f"http://{lan}:{http}") and _over_https(f"https://{lan}:{https}", ca)


@pytest.mark.slow
@NO_POSTGRES
def test_run_is_the_whole_install_with_its_own_postgres(tmp_path):
    """A Mac's one service: nothing running beforehand. It makes its own
    PostgreSQL, the control database and migrates it, serves the API (through
    its own nginx, where there's one), and stops it all, PostgreSQL last."""
    lumiverb_server = Path(sys.executable).parent / "lumiverb-server"
    pg_port, api_port, web_port, tls_port = free_port(), free_port(), free_port(), free_port()
    url = f"postgresql+psycopg2://lumiverb:its-own-password@127.0.0.1:{pg_port}"
    lines = [f"CONTROL_PLANE_DATABASE_URL={url}/control_plane", f"TENANT_DATABASE_URL_TEMPLATE={url}/{{tenant_id}}",
             f"DATA_DIR={tmp_path / 'data'}", f"API_PORT={api_port}", "API_LISTEN_HOST=127.0.0.1",
             "LUMIVERB_API_WORKERS=1", "ADMIN_KEY=test-admin-run", "JWT_SECRET=test-jwt-secret-of-at-least-32-bytes",
             f"PG_BIN={postgres_bin()}"]
    nginx = _nginx_bin()
    if nginx:
        conf = tmp_path / "nginx.conf"
        conf.write_text(nginx_conf(nginx_site(listen=(network.include_line(tmp_path / "data"),), server_name="_",
                                              root=tmp_path / "web", upstream=f"http://127.0.0.1:{api_port}")))
        lines += [f"NGINX_BIN={nginx}", f"NGINX_CONFIG={conf}", f"WEB_PORT={web_port}", f"WEB_TLS_PORT={tls_port}"]
    env_file = tmp_path / "env"
    env_file.write_text("\n".join(lines) + "\n")
    out = tmp_path / "service.log"
    # launchd's: none of the install's settings, and no locale.
    clean_env = {k: v for k, v in os.environ.items()
                 if not k.startswith(("CONTROL_PLANE", "TENANT_DATABASE", "LUMIVERB_", "API_", "DATA_DIR", "PG",
                                      "LANG", "LC_"))}
    with out.open("w") as log:
        service = subprocess.Popen([str(lumiverb_server), "run", "--env", str(env_file)],
                                   stdout=log, stderr=subprocess.STDOUT, env=clean_env)
    try:
        deadline = time.monotonic() + 120
        while not _healthy(api_port):
            assert service.poll() is None, out.read_text()
            assert time.monotonic() < deadline, out.read_text()
            time.sleep(0.5)
        if nginx:
            assert _setup_needed(f"http://127.0.0.1:{web_port}")
            _other_devices_may_use_it(tmp_path / "data", web_port, tls_port, out)
    finally:
        service.send_signal(signal.SIGTERM)
        try:
            code = service.wait(timeout=180)
        except subprocess.TimeoutExpired:
            service.kill()
            raise

    said = out.read_text()
    assert code == 0, said
    for line in ("Made Lumiverb's database", "postgres started", "Created the control database", "Control plane"):
        assert line in said, said
    order = ["scheduler stopped", "api stopped", *(["nginx stopped"] if nginx else []), "postgres stopped"]
    # The last of each: nginx also stopped once to restart.
    assert [said.rindex(line) for line in order] == sorted(said.rindex(line) for line in order), said
