"""deploy-api.sh settings: what a first install and a rerun use (ADR-016 phase 2).

`--dry-run` prints the settings the script resolved and stops before touching
anything, so these run as an ordinary user. LUMIVERB_CONF_DIR stands in for
/etc/lumiverb.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parents[1]
DEPLOY_API = REPO / "scripts" / "deploy-api.sh"


def _settings(tmp_path: Path, *args: str, env_file: str | None = None, path_prefix: Path | None = None) -> dict[str, str]:
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    if env_file is not None:
        (conf / "env").write_text(env_file)
    env = {
        **os.environ,
        "LUMIVERB_CONF_DIR": str(conf),
    }
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    out = subprocess.run(
        ["bash", str(DEPLOY_API), *args, "--dry-run"],
        capture_output=True, text=True, env=env, timeout=30, check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


def test_first_install_without_any_postgres_gets_18(tmp_path):
    # The brain has no /etc/postgresql (Resolve's database runs in Docker).
    s = _settings(tmp_path, "--app-host", "http://192.168.86.166")
    assert s["PG_VERSION"] == "18"
    assert s["DATA_DIR"] == "/var/lib/lumiverb"
    assert s["BRANCH"] == "main"
    assert s["NO_FIREWALL"] == "false"


def test_first_install_takes_its_flags(tmp_path):
    s = _settings(
        tmp_path, "--app-host", "http://x", "--data-dir", "/mnt/ssd2/lumiverb", "--branch", "feat/brain",
        "--no-firewall", "--pg-port", "5434", "--pg-version", "17",
    )
    assert s["DATA_DIR"] == "/mnt/ssd2/lumiverb"
    assert s["BRANCH"] == "feat/brain"
    assert s["NO_FIREWALL"] == "true"
    assert s["PG_PORT"] == "5434"
    assert s["PG_VERSION"] == "17"


def test_dry_run_creates_nothing(tmp_path):
    data = tmp_path / "data"
    _settings(tmp_path, "--app-host", "http://x", "--data-dir", str(data))
    assert not data.exists()


REMEMBERED = """\
DATA_DIR=/mnt/ssd2/lumiverb
API_PORT=8100
PG_PORT=5434
PG_VERSION=18
QUICKWIT_PORT=7290
APP_HOST=http://192.168.86.166
BRANCH=feat/brain
NO_FIREWALL=true
"""


def test_rerun_without_flags_keeps_everything(tmp_path):
    # Without this, a rerun moved DATA_DIR back to /var/lib/lumiverb (search
    # empty, artifacts 404), checked out main and turned ufw on (port 80 shut).
    s = _settings(tmp_path, env_file=REMEMBERED)
    assert s["DATA_DIR"] == "/mnt/ssd2/lumiverb"
    assert s["BRANCH"] == "feat/brain"
    assert s["NO_FIREWALL"] == "true"
    assert s["PG_VERSION"] == "18"
    assert s["PG_PORT"] == "5434"
    assert s["API_PORT"] == "8100"
    assert s["QUICKWIT_PORT"] == "7290"
    assert s["APP_HOST"] == "http://192.168.86.166"


def test_flags_beat_remembered_values(tmp_path):
    s = _settings(tmp_path, "--branch", "main", "--data-dir", "/srv/lv", env_file=REMEMBERED)
    assert s["BRANCH"] == "main"
    assert s["DATA_DIR"] == "/srv/lv"


def test_earlier_install_without_pg_version_keeps_the_cluster_on_its_port(tmp_path):
    # Installs from before PG_VERSION was remembered: use the version of the
    # cluster on our port, not whichever is newest, and never 18 by default
    # (that would start an empty database beside the real one).
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lsclusters = bin_dir / "pg_lsclusters"
    lsclusters.write_text(
        "#!/bin/sh\n"
        "echo '17  other 5440 online postgres /var/lib/postgresql/17/other /var/log/x'\n"
        "echo '16  main  5432 online postgres /var/lib/postgresql/16/main /var/log/y'\n"
    )
    lsclusters.chmod(0o755)
    s = _settings(tmp_path, env_file="PG_PORT=5432\nDATA_DIR=/var/lib/lumiverb\nAPP_HOST=https://lv.example.org\n", path_prefix=bin_dir)
    assert s["PG_VERSION"] == "16"


def test_env_file_remembers_the_rerun_settings():
    text = DEPLOY_API.read_text()
    env_block = text.split('cat > "$ENV_FILE" <<ENVEOF', 1)[1].split("ENVEOF", 1)[0]
    for key in ("DATA_DIR", "PG_PORT", "PG_VERSION", "API_PORT", "QUICKWIT_PORT", "APP_HOST", "BRANCH", "NO_FIREWALL"):
        assert f"\n{key}=${{" in env_block, f"{key} isn't written to /etc/lumiverb/env"


def _write_env(env: Path) -> None:
    """Run deploy-api.sh's env-file writing against `env`."""
    text = DEPLOY_API.read_text()
    start = text.index("# Settings added by hand")
    block = text[start:text.index('chmod 600 "$ENV_FILE"', start)]
    script = (
        f'ENV_FILE="{env}"; DB_URL=postgresql://app:pw@127.0.0.1:5434; PG_DB=control_plane\n'
        "ADMIN_KEY=a; API_SECRET_KEY=b; JWT_SECRET=c; DATA_DIR=/mnt/ssd2/lumiverb; QW_PORT=7290\n"
        "API_LISTEN_HOST=127.0.0.1; API_PORT=8100; PG_PORT=5434; PG_VERSION=18; BRANCH=feat/brain\n"
        "NO_FIREWALL=true; APP_HOST=http://192.168.86.166; DOMAIN=\n"
        + block
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr


def test_a_rerun_keeps_settings_added_to_the_env_file_by_hand(tmp_path):
    # Forgot-password needs SMTP_* filled in by hand; a rerun dropped them.
    env = tmp_path / "env"
    env.write_text("ADMIN_KEY=a\nAPI_PORT=8000\nSMTP_HOST=smtp.fastmail.com\n# a note\nSMTP_PASSWORD=s3cret=x\n")
    _write_env(env)
    _write_env(env)  # reruns add nothing
    lines = env.read_text().splitlines()
    assert lines.count("SMTP_HOST=smtp.fastmail.com") == 1
    assert lines.count("SMTP_PASSWORD=s3cret=x") == 1
    # What the script writes isn't kept twice, and its values win.
    assert [line for line in lines if line.startswith("API_PORT=")] == ["API_PORT=8100"]
    assert [line for line in lines if line.startswith("ADMIN_KEY=")] == ["ADMIN_KEY=a"]
    fresh = tmp_path / "fresh"
    _write_env(fresh)
    assert "kept" not in fresh.read_text()


def test_worker_caches_live_in_the_data_dir():
    # The worker's home is on the root disk, beside Postgres and Docker; its
    # proxy caches belong on the data disk.
    text = DEPLOY_API.read_text()
    worker_unit = text.split("Description=Lumiverb worker", 1)[1].split("UNIT", 1)[0]
    assert "Environment=XDG_CACHE_HOME=${DATA_DIR}/cache" in worker_unit


def test_api_buffers_uploads_on_the_data_disk():
    # Starlette spools large uploads (analysis proxies) to TMPDIR; the API's
    # PrivateTmp /tmp is RAM on the brain.
    text = DEPLOY_API.read_text()
    api_unit = text.split("Description=Lumiverb API Server", 1)[1].split("UNIT", 1)[0]
    assert "Environment=TMPDIR=${DATA_DIR}/tmp" in api_unit
    # PrivateTmp emptied /tmp on every start; uploads a killed API left there go the same way.
    assert "ExecStartPre=-/usr/bin/find ${DATA_DIR}/tmp -mindepth 1 -delete" in api_unit
    assert "ReadWritePaths=${DATA_DIR}" in api_unit
    assert '"$DATA_DIR"/tmp' in text.split('step "Creating service user and directories"', 1)[1].split("ok ", 1)[0]


UPDATE_API = REPO / "scripts" / "update-api.sh"

API_UNIT = """\
[Service]
EnvironmentFile=/etc/lumiverb/env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/lumiverb/.venv/bin/uvicorn src.server.api.main:app
PrivateTmp=true
ReadWritePaths=/mnt/ssd2/lumiverb
"""


def _update_api_unit(tmp_path: Path, unit: Path) -> None:
    """Run update-api.sh's API-unit step against `unit`."""
    text = UPDATE_API.read_text()
    block = text.split('step "Updating the API unit"', 1)[1].split("# ----", 1)[0]
    script = (
        'step() { :; }; ok() { :; }; warn() { :; }; systemctl() { :; }\n'
        "DATA_DIR=/mnt/ssd2/lumiverb\n"
        + block.replace("/etc/systemd/system/lumiverb-api.service", str(unit))
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr


def test_update_moves_an_existing_api_unit_s_tmpdir_to_the_data_disk(tmp_path):
    unit = tmp_path / "lumiverb-api.service"
    unit.write_text(API_UNIT)
    _update_api_unit(tmp_path, unit)
    _update_api_unit(tmp_path, unit)  # reruns change nothing
    lines = unit.read_text().splitlines()
    assert lines.count("Environment=TMPDIR=/mnt/ssd2/lumiverb/tmp") == 1
    assert lines.index("Environment=TMPDIR=/mnt/ssd2/lumiverb/tmp") == lines.index("Environment=PYTHONUNBUFFERED=1") + 1
    assert lines.count("ExecStartPre=-/usr/bin/find /mnt/ssd2/lumiverb/tmp -mindepth 1 -delete") == 1


def test_update_makes_the_api_tmpdir():
    text = UPDATE_API.read_text()
    data_step = text.split('step "Ensuring data directory"', 1)[1].split("# ----", 1)[0]
    assert '"$DATA_DIR"/tmp' in data_step
    assert 'chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"' in data_step


WORKER_UNIT = """\
[Service]
EnvironmentFile=/etc/lumiverb/env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/lumiverb/.venv/bin/lumiverb worker
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/mnt/ssd2/lumiverb
"""


def _update_worker_unit(tmp_path: Path, unit: Path, env_text: str) -> str:
    """Run update-api.sh's worker-unit step against `unit`; returns its output."""
    env = tmp_path / "env"
    env.write_text(env_text)
    text = UPDATE_API.read_text()
    block = text.split('step "Updating the worker unit"', 1)[1].split("# ----", 1)[0]
    script = (
        'step() { :; }; ok() { :; }; warn() { echo "warn: $1"; }; systemctl() { :; }\n'
        f'ENV_FILE="{env}"; APP_DIR=/opt/lumiverb; SVC_HOME=/var/lib/lumiverb\n'
        + block.replace("/etc/systemd/system/lumiverb-worker.service", str(unit))
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


def test_update_puts_the_worker_s_caches_on_the_data_disk(tmp_path):
    unit = tmp_path / "lumiverb-worker.service"
    unit.write_text(WORKER_UNIT)
    _update_worker_unit(tmp_path, unit, "DATA_DIR=/mnt/ssd2/lumiverb\n")
    _update_worker_unit(tmp_path, unit, "DATA_DIR=/mnt/ssd2/lumiverb\n")  # reruns change nothing
    lines = unit.read_text().splitlines()
    assert lines.count("Environment=XDG_CACHE_HOME=/mnt/ssd2/lumiverb/cache") == 1
    assert lines.count("ReadWritePaths=/mnt/ssd2/lumiverb /var/lib/lumiverb") == 1


def test_update_without_a_data_dir_leaves_the_worker_s_caches_alone(tmp_path):
    # XDG_CACHE_HOME=/cache would be read-only under ProtectSystem=strict:
    # the worker couldn't make its lock, and wouldn't start.
    unit = tmp_path / "lumiverb-worker.service"
    unit.write_text(WORKER_UNIT)
    out = _update_worker_unit(tmp_path, unit, "API_PORT=8100\n")
    assert "XDG_CACHE_HOME" not in unit.read_text()
    assert "No DATA_DIR" in out


def test_a_manual_worker_run_finds_the_service_s_lock():
    # `sudo -u lumiverb -H lumiverb worker --once` gets neither the unit's
    # XDG_CACHE_HOME nor the env file (root only); the CLI config carries it.
    text = DEPLOY_API.read_text()
    env_block = text.split('cat > "$ENV_FILE" <<ENVEOF', 1)[1].split("ENVEOF", 1)[0]
    assert "\nXDG_CACHE_HOME=${DATA_DIR}/cache\n" in env_block
    assert 'lumiverb" config set --cache-home "${DATA_DIR}/cache"' in text


def _update_data_dir(tmp_path: Path, env_text: str) -> tuple[str, list[str]]:
    """Run update-api.sh's data-dir step against an env file; returns it and the sudo calls."""
    env = tmp_path / "env"
    env.write_text(env_text)
    calls = tmp_path / "sudo-calls"
    calls.touch()
    text = UPDATE_API.read_text()
    block = text.split('step "Ensuring data directory"', 1)[1].split("# ----", 1)[0]
    script = (
        'step() { :; }; ok() { :; }; warn() { :; }; chown() { :; }\n'
        f'sudo() {{ echo "$*" >> "{calls}"; }}\n'
        f'ENV_FILE="{env}"; SVC_USER=lumiverb; APP_DIR=/opt/lumiverb\n'
        + block
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    return env.read_text(), calls.read_text().splitlines()


def test_update_points_caches_and_the_worker_lock_at_the_data_disk(tmp_path):
    data = tmp_path / "data"
    _update_data_dir(tmp_path, f"DATA_DIR={data}\nAPI_PORT=8100\n")
    env, calls = _update_data_dir(tmp_path, (tmp_path / "env").read_text())  # reruns add nothing
    assert env.splitlines().count(f"XDG_CACHE_HOME={data}/cache") == 1
    assert f"-u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb config set --cache-home {data}/cache" in calls


def test_update_without_a_data_dir_leaves_caches_alone(tmp_path):
    env, calls = _update_data_dir(tmp_path, "API_PORT=8100\n")
    assert env == "API_PORT=8100\n"
    assert calls == []


DEPLOY_WEB = REPO / "scripts" / "deploy-web.sh"


def _web_settings(tmp_path: Path, *args: str, env_file: str | None = None) -> dict[str, str]:
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    if env_file is not None:
        (conf / "env").write_text(env_file)
    out = subprocess.run(
        ["bash", str(DEPLOY_WEB), "--domain", "_", "--api-upstream", "http://127.0.0.1:8100", *args, "--dry-run"],
        capture_output=True, text=True, env={**os.environ, "LUMIVERB_CONF_DIR": str(conf)}, timeout=30, check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


def test_web_follows_the_api_install_on_the_same_machine(tmp_path):
    # Both scripts deploy /opt/lumiverb: a web deploy without --branch must
    # not switch the API's code to main, nor turn ufw on.
    s = _web_settings(tmp_path, env_file=REMEMBERED)
    assert s["BRANCH"] == "feat/brain"
    assert s["NO_FIREWALL"] == "true"


needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")


@needs_permissions
@pytest.mark.parametrize("script", [
    [str(DEPLOY_API)],
    [str(DEPLOY_WEB), "--domain", "_", "--api-upstream", "http://127.0.0.1:8100"],
], ids=["api", "web"])
@pytest.mark.parametrize("locked", ["file", "folder"])
def test_a_dry_run_that_cannot_read_the_settings_says_so(tmp_path, script, locked):
    # /etc/lumiverb/env is 600 in a 750 folder. Read as nobody, it looked
    # absent, and the dry run printed defaults: Postgres on 5432 (Resolve's),
    # branch main.
    conf = tmp_path / "conf"
    conf.mkdir()
    env_file = conf / "env"
    env_file.write_text(REMEMBERED)
    (env_file if locked == "file" else conf).chmod(0)
    try:
        out = subprocess.run(["bash", *script, "--dry-run"], capture_output=True, text=True, timeout=30,
                             env={**os.environ, "LUMIVERB_CONF_DIR": str(conf)}, check=False)
    finally:
        conf.chmod(0o755)
        env_file.chmod(0o644)
    assert out.returncode != 0
    assert f"Run with sudo to see the settings remembered in {env_file}" in out.stderr
    assert "BRANCH=" not in out.stdout


@pytest.mark.parametrize(("enabled", "flag", "worker"), [
    (True, (), "true"), (False, (), "false"), (False, ("--worker",), "true"),
])
def test_a_dry_run_reports_the_worker_as_a_real_run_decides(tmp_path, enabled, flag, worker):
    # A real run keeps an installed worker without --worker.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text("#!/bin/sh\n" + ('[ "$1 $2" = "is-enabled lumiverb-worker" ] && exit 0\n' if enabled else "")
                         + "exit 1\n")
    systemctl.chmod(0o755)
    assert _settings(tmp_path, *flag, env_file=REMEMBERED, path_prefix=bin_dir)["WORKER"] == worker


def test_the_real_run_uses_the_worker_setting_the_dry_run_shows():
    text = DEPLOY_API.read_text()
    deps = text.split('step "Installing Python dependencies"', 1)[1].split("# ----", 1)[0]
    assert "is-enabled" not in deps
    assert text.index("systemctl is-enabled lumiverb-worker") < text.index('if [[ "$DRY_RUN" == "true" ]]')


def test_web_on_its_own_machine_uses_defaults_and_flags(tmp_path):
    assert _web_settings(tmp_path)["BRANCH"] == "main"
    assert _web_settings(tmp_path)["NO_FIREWALL"] == "false"
    s = _web_settings(tmp_path, "--branch", "x", "--no-firewall", env_file=REMEMBERED.replace("BRANCH=feat/brain", "BRANCH=y"))
    assert s["BRANCH"] == "x"
    assert s["NO_FIREWALL"] == "true"


def _deploy_web_site(tmp_path: Path, upstream: str = "http://127.0.0.1:8100") -> str:
    """The nginx site deploy-web.sh writes for --domain _."""
    text = DEPLOY_WEB.read_text()
    start = text.index("cat > /etc/nginx/sites-available/lumiverb <<NGINX")
    end = text.index("\nNGINX\n", start) + len("\nNGINX\n")
    site = tmp_path / "fresh-site"
    script = (f'API_UPSTREAM={upstream}; LISTEN="listen 80 default_server;"; DOMAIN=_; APP_DIR=/opt/lumiverb\n'
              + text[start:end].replace("/etc/nginx/sites-available/lumiverb", str(site)))
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return site.read_text()


def _location(site: str, prefix: str) -> list[str]:
    lines = site.splitlines()
    start = lines.index(f"    location {prefix} {{")
    return [line.strip() for line in lines[start + 1:lines.index("    }", start)]]


def test_playback_streams_straight_through_and_stays_out_of_the_access_log(tmp_path):
    # Buffered, a bytes=0- request for a multi-GB proxy spooled up to 1 GB to
    # /var/lib/nginx on the root disk; logged, its bearer token (good for
    # hours) sat in /var/log/nginx.
    site = _deploy_web_site(tmp_path)
    stream = _location(site, "/v1/stream/")
    for directive in ("proxy_buffering off;", "proxy_request_buffering off;", "access_log off;"):
        assert directive in stream
    same = ("proxy_pass ", "proxy_http_version ", "proxy_set_header ", "proxy_connect_timeout ", "proxy_read_timeout ")
    assert [d for d in _location(site, "/v1/") if d.startswith(same)] == [d for d in stream if d.startswith(same)]


# What deploy-web.sh wrote before playback streams had their own location.
OLD_SITE = """\
server {
    listen 80 default_server;
    server_name _;

    root /opt/lumiverb/src/ui/web/dist;
    index index.html;

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer-when-downgrade always;

    location /v1/ {
        proxy_pass http://127.0.0.1:8100;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 100m;
        proxy_connect_timeout 60s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
"""

UPDATE_WEB = REPO / "scripts" / "update-web.sh"


def _update_web_site(site: Path) -> None:
    """Run update-web.sh's nginx-site step against `site`."""
    text = UPDATE_WEB.read_text()
    block = text.split('step "Updating the nginx site"', 1)[1].split("# ----", 1)[0]
    script = 'step() { :; }; ok() { :; }; warn() { :; }\n' + block.replace("/etc/nginx/sites-available/lumiverb", str(site))
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr


def test_update_gives_an_existing_site_the_playback_location_once(tmp_path):
    site = tmp_path / "lumiverb"
    site.write_text(OLD_SITE)
    _update_web_site(site)
    _update_web_site(site)  # reruns change nothing
    assert site.read_text() == _deploy_web_site(tmp_path)


def test_update_leaves_a_site_without_the_api_location_alone(tmp_path):
    site = tmp_path / "lumiverb"
    site.write_text("server {\n    listen 80;\n}\n")
    _update_web_site(site)
    assert site.read_text() == "server {\n    listen 80;\n}\n"
    _update_web_site(tmp_path / "missing")  # no site, nothing to do


@pytest.mark.parametrize("name,next_step", [("update-api.sh", 'step "Updating Python dependencies"'),
                                            ("update-web.sh", 'step "Rebuilding web UI"')])
def test_update_scripts_rerun_themselves_after_pulling(name, next_step):
    # bash keeps executing the copy of the script it started with; git pull
    # writes a new file. Without a re-exec, steps added by the update only
    # run on the update after it.
    text = (REPO / "scripts" / name).read_text()
    pull = text.index("git pull")
    reexec = text.index(f'exec bash "$APP_DIR/scripts/{name}" "$@"')
    assert pull < reexec < text.index(next_step)
    guard = text[pull:reexec]
    assert "LUMIVERB_UPDATE_REEXEC" in guard


def test_update_reexec_runs_the_pulled_copy_once(tmp_path):
    """The guard re-execs into the new copy exactly once."""
    text = UPDATE_API.read_text()
    start = text.index('if [[ "${LUMIVERB_UPDATE_REEXEC:-}"')
    block = text[start:text.index("fi\n", start) + 3]
    new_copy = tmp_path / "scripts" / "update-api.sh"
    new_copy.parent.mkdir()
    new_copy.write_text('echo "new copy, reexec=$LUMIVERB_UPDATE_REEXEC"; ' + block.replace("exec", "echo would-exec") + "\n")
    script = f'APP_DIR={tmp_path}\nset -- --flag\n' + block + 'echo "old copy kept going"\n'
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["new copy, reexec=1"]


@pytest.mark.parametrize("name", ["deploy-api.sh", "deploy-web.sh", "update-api.sh", "update-web.sh"])
def test_a_failed_step_says_where_it_stopped(name, tmp_path):
    text = (REPO / "scripts" / name).read_text()
    trap = next(line for line in text.splitlines() if line.startswith("trap ") and line.endswith(" ERR"))
    script = "set -euo pipefail\nRED=''; NC=''\n" + trap + "\necho before\nfalse\necho after\n"
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode != 0
    assert "after" not in out.stdout
    assert "Stopped" in out.stderr and "false" in out.stderr
