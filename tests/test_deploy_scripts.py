"""deploy-api.sh settings: what a first install and a rerun use (ADR-016 phase 2).

`--dry-run` prints the settings the script resolved and stops before touching
anything, so these run as an ordinary user. LUMIVERB_CONF_DIR stands in for
/etc/lumiverb.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import LINUX_INSTALLER

pytestmark = [pytest.mark.fast, LINUX_INSTALLER]

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
    # Prod has no /etc/postgresql (Resolve's database runs in Docker).
    s = _settings(tmp_path, "--app-host", "http://192.168.86.166")
    assert s["PG_VERSION"] == "18"
    assert s["DATA_DIR"] == "/var/lib/lumiverb"
    assert s["BRANCH"] == "main"
    assert s["NO_FIREWALL"] == "false"


def test_first_install_takes_its_flags(tmp_path):
    s = _settings(
        tmp_path, "--app-host", "http://x", "--data-dir", "/mnt/ssd2/lumiverb", "--branch", "feat/other",
        "--no-firewall", "--pg-port", "5434", "--pg-version", "17",
    )
    assert s["DATA_DIR"] == "/mnt/ssd2/lumiverb"
    assert s["BRANCH"] == "feat/other"
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
BRANCH=feat/other
NO_FIREWALL=true
"""


def test_rerun_without_flags_keeps_everything(tmp_path):
    # Without this, a rerun moved DATA_DIR back to /var/lib/lumiverb (search
    # empty, artifacts 404), checked out main and turned ufw on (port 80 shut).
    s = _settings(tmp_path, env_file=REMEMBERED)
    assert s["DATA_DIR"] == "/mnt/ssd2/lumiverb"
    assert s["BRANCH"] == "feat/other"
    assert s["NO_FIREWALL"] == "true"
    assert s["PG_VERSION"] == "18"
    assert s["PG_PORT"] == "5434"
    assert s["API_PORT"] == "8100"
    assert s["APP_HOST"] == "http://192.168.86.166"
    # An earlier install's Quickwit port is nothing to remember: search is PostgreSQL's (ADR-019).
    assert not [key for key in s if "QUICKWIT" in key]


def test_flags_beat_remembered_values(tmp_path):
    s = _settings(tmp_path, "--branch", "main", "--data-dir", "/srv/lv", env_file=REMEMBERED)
    assert s["BRANCH"] == "main"
    assert s["DATA_DIR"] == "/srv/lv"


def test_firewall_turns_a_remembered_no_firewall_back_off(tmp_path):
    # Remembered once, --no-firewall could never be undone.
    assert _settings(tmp_path, "--firewall", env_file=REMEMBERED)["NO_FIREWALL"] == "false"
    assert _settings(tmp_path, "--no-firewall", "--firewall", env_file=REMEMBERED)["NO_FIREWALL"] == "false"
    assert _settings(tmp_path, "--firewall", "--no-firewall")["NO_FIREWALL"] == "true"
    assert _web_settings(tmp_path, "--firewall", env_file=REMEMBERED)["NO_FIREWALL"] == "false"


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
    for key in ("DATA_DIR", "PG_PORT", "PG_VERSION", "API_PORT", "APP_HOST", "BRANCH", "NO_FIREWALL"):
        assert f"\n{key}=${{" in env_block, f"{key} isn't written to /etc/lumiverb/env"


def _write_env(env: Path, settings: str = "") -> None:
    """Run deploy-api.sh's env-file writing against `env` (settings: more of the script's variables)."""
    text = DEPLOY_API.read_text()
    start = text.index("# Settings added by hand")
    block = text[start:text.index('chmod 600 "$ENV_FILE"', start)]
    script = (
        f'ENV_FILE="{env}"; DB_URL=postgresql://app:pw@127.0.0.1:5434; PG_DB=control_plane\n'
        "ADMIN_KEY=a; API_SECRET_KEY=b; JWT_SECRET=c; DATA_DIR=/mnt/ssd2/lumiverb\n"
        "API_LISTEN_HOST=127.0.0.1; API_PORT=8100; PG_PORT=5434; PG_VERSION=18; BRANCH=feat/other\n"
        "NO_FIREWALL=true; APP_HOST=http://192.168.86.166; DOMAIN=\n"
        + settings + "\n"
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


def test_a_rerun_drops_what_earlier_versions_wrote_for_quickwit(tmp_path):
    # Its own lines from before ADR-019, which nothing reads now: not kept as
    # if added by hand. What was added by hand still is.
    env = tmp_path / "env"
    env.write_text("ADMIN_KEY=a\nQUICKWIT_URL=http://127.0.0.1:7290\nQUICKWIT_ENABLED=true\n"
                   "QUICKWIT_PORT=7290\nSMTP_HOST=smtp.fastmail.com\n")
    _write_env(env)
    text = env.read_text()
    assert "QUICKWIT" not in text
    assert text.splitlines().count("SMTP_HOST=smtp.fastmail.com") == 1


def test_scheduler_caches_live_in_the_data_dir():
    # The service's home is on the root disk, beside Postgres and Docker; its
    # proxy caches belong on the data disk.
    text = DEPLOY_API.read_text()
    unit = text.split("Description=Lumiverb scheduler", 1)[1].split("UNIT", 1)[0]
    assert "Environment=XDG_CACHE_HOME=${DATA_DIR}/cache" in unit
    assert "ExecStart=${APP_DIR}/.venv/bin/python -m src.server.scheduler" in unit
    assert "KillMode=mixed" in unit
    assert "lumiverb-worker.service <<UNIT" not in text


def test_api_buffers_uploads_on_the_data_disk():
    # Starlette spools large uploads (analysis proxies) to TMPDIR; the API's
    # PrivateTmp /tmp is RAM on prod.
    text = DEPLOY_API.read_text()
    api_unit = text.split("Description=Lumiverb API Server", 1)[1].split("UNIT", 1)[0]
    assert "Environment=TMPDIR=${DATA_DIR}/tmp" in api_unit
    # PrivateTmp emptied /tmp on every start; uploads a killed API left there go the same way.
    assert "ExecStartPre=-/usr/bin/find ${DATA_DIR}/tmp -mindepth 1 -delete" in api_unit
    assert "ReadWritePaths=${DATA_DIR}" in api_unit
    assert '"$DATA_DIR"/tmp' in text.split('step "Creating service user and directories"', 1)[1].split("ok ", 1)[0]


UPDATE_API = REPO / "scripts" / "update-api.sh"


def _update_helpers() -> str:
    """update-api.sh's helpers its steps use: SVC_ENV and as_svc, svc_folder, data_dir_shape and read_data_dir."""
    text = UPDATE_API.read_text()
    return text[text.index("SVC_ENV=("):text.index('[[ "$(id -u)" -eq 0 ]] || fail "Run as root"')]


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
        "DATA_DIR=/mnt/ssd2/lumiverb; APP_DIR=/opt/lumiverb\n"
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


def test_an_update_starts_the_api_as_a_new_install_does_with_no_process_of_it_dumpable(tmp_path):
    # uvicorn's own process holds the env file's secrets and isn't the app: python -m src.server.api makes it not
    # dumpable first (the workers do it themselves, main.py). Once; a unit edited by hand is said, not changed.
    unit = tmp_path / "lumiverb-api.service"
    unit.write_text(OLD_API_UNIT.format(data="/mnt/ssd2/lumiverb"))
    _update_api_unit(tmp_path, unit)
    _update_api_unit(tmp_path, unit)
    [start] = [line for line in unit.read_text().splitlines() if line.startswith("ExecStart=")]
    deployed = DEPLOY_API.read_text().split("cat > /etc/systemd/system/lumiverb-api.service <<UNIT", 1)[1]
    [fresh] = [line for line in deployed.split("\nUNIT\n", 1)[0].splitlines() if line.startswith("ExecStart=")]
    assert start == fresh.replace("${APP_DIR}", "/opt/lumiverb").replace("\\$", "$") == (
        "ExecStart=/opt/lumiverb/.venv/bin/python -m src.server.api src.server.api.main:app "
        "--host ${API_LISTEN_HOST} --port ${API_PORT} --workers 2")
    text = UPDATE_API.read_text()
    block = text.split('step "Updating the API unit"', 1)[1].split("# ----", 1)[0]
    unit.write_text("[Service]\nExecStart=/usr/local/bin/my-uvicorn src.server.api.main:app\n")
    out = subprocess.run(["bash", "-euo", "pipefail", "-c",
                          'step() { :; }; ok() { :; }; warn() { echo "warn: $1"; }; systemctl() { :; }\n'
                          "DATA_DIR=; APP_DIR=/opt/lumiverb; ENV_FILE=/etc/lumiverb/env\n"
                          + block.replace("/etc/systemd/system/lumiverb-api.service", str(unit))],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "readable to the other processes of its user" in out.stdout
    assert unit.read_text() == "[Service]\nExecStart=/usr/local/bin/my-uvicorn src.server.api.main:app\n"


def test_update_makes_the_api_tmpdir():
    text = UPDATE_API.read_text()
    data_step = text.split('step "Ensuring data directory"', 1)[1].split("# ----", 1)[0]
    assert '"$DATA_DIR"/tmp' in data_step
    assert 'chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"' in data_step


def _update_migrations(tmp_path: Path, backup_rc: int) -> tuple[str, list[str]]:
    """Run update-api.sh's migrations step with sudo stubbed (the backup
    answering backup_rc); returns its output and each sudo call, with the
    database URL and DATA_DIR the backup was handed."""
    env = tmp_path / "env"
    env.write_text("CONTROL_PLANE_DATABASE_URL=postgresql://app@db/control_plane\nDATA_DIR=/mnt/ssd2/lumiverb\n")
    calls = tmp_path / "sudo-calls"
    calls.write_text("")
    text = UPDATE_API.read_text()
    block = text.split('step "Running migrations"', 1)[1].split("# ----", 1)[0]
    script = (
        'step() { :; }; ok() { echo "ok: $1"; }; warn() { echo "warn: $1"; }; fail() { echo "fail: $1"; exit 1; }\n'
        "systemctl() { return 1; }\n"
        f'sudo() {{ echo "$* | ${{CONTROL_PLANE_DATABASE_URL:-}} ${{DATA_DIR:-}}" >> "{calls}";'
        f' [[ "$*" != *"backup --if-migrating"* ]] || return {backup_rc}; }}\n'
        f'ENV_FILE="{env}"; APP_DIR=/opt/lumiverb; SVC_USER=lumiverb; UV_BIN=/usr/local/bin/uv\n'
        + _update_helpers() + block
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout + out.stderr, calls.read_text().splitlines()


def test_an_update_backs_up_before_migrating(tmp_path):
    # As a Mac's service does: the service user's lumiverb-server, only when there's something to migrate.
    out, calls = _update_migrations(tmp_path, 0)
    assert calls[0] == ("-u lumiverb --preserve-env=CONTROL_PLANE_DATABASE_URL,DATA_DIR "
                        "/opt/lumiverb/.venv/bin/lumiverb-server backup --if-migrating"
                        " | postgresql://app@db/control_plane /mnt/ssd2/lumiverb")
    assert "alembic-control.ini upgrade head" in calls[1] and "migrate.sh" in calls[2]
    assert "ok: Backed up before migrating" in out and "warn:" not in out


def test_an_update_whose_backup_fails_says_so_and_migrates(tmp_path):
    out, calls = _update_migrations(tmp_path, 1)
    assert "warn: No backup before migrating" in out
    assert "alembic-control.ini upgrade head" in calls[1] and "migrate.sh" in calls[2]


def test_the_tenant_migrations_uv_reads_none_of_the_service_users_settings(tmp_path):
    _, calls = _update_migrations(tmp_path, 0)
    assert calls[2].startswith("-u lumiverb --preserve-env=CONTROL_PLANE_DATABASE_URL,ALEMBIC_CONTROL_URL,UV_BIN,"
                               "DATA_DIR,LUMIVERB_GEONAMES_URL env GIT_CONFIG_GLOBAL=/dev/null "
                               "npm_config_userconfig=/dev/null UV_NO_CONFIG=1 bash /opt/lumiverb/scripts/migrate.sh")


def _update_scheduler(tmp_path: Path, env_text: str, *, processing: bool = True,
                      worker: bool = True) -> tuple[str, list[str], Path]:
    """Run update-api.sh's scheduler step; returns its output, the systemctl
    and chown calls and where the unit was written."""
    env = tmp_path / "env"
    env.write_text(env_text)
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    if worker:
        (units / "lumiverb-worker.service").write_text("[Service]\nExecStart=/opt/lumiverb/.venv/bin/lumiverb worker\n")
    calls = tmp_path / "systemctl-calls"
    calls.write_text("")
    text = UPDATE_API.read_text()
    block = text.split('step "Installing the scheduler', 1)[1].split("\n", 1)[1].split("# ----", 1)[0]
    script = (
        'step() { :; }; ok() { echo "ok: $1"; }; warn() { echo "warn: $1"; }\n'
        f'chown() {{ echo "chown $*" >> "{calls}"; }}\n'
        'fail() { echo "fail: $1"; exit 1; }\n'
        # Each call notes whether the worker's stop then reaches only its main process.
        f'systemctl() {{ echo "$*$(grep -qs KillMode=mixed {units}/lumiverb-worker.service.d/*.conf'
        f' && echo " [worker: KillMode=mixed]")" >> "{calls}"; }}\n'
        f'ENV_FILE="{env}"; APP_DIR=/opt/lumiverb; SVC_HOME=/var/lib/lumiverb; SVC_USER=lumiverb\n'
        f'PROCESSING={"true" if processing else "false"}\n'
        + _update_helpers() + block.replace("/etc/systemd/system/", f"{units}/")
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    return out.stdout + out.stderr, calls.read_text().splitlines(), units


def test_update_replaces_the_worker_with_the_scheduler(tmp_path):
    data = tmp_path / "data"
    out, calls, units = _update_scheduler(tmp_path, f"DATA_DIR={data}\n")
    unit = (units / "lumiverb-scheduler.service").read_text()
    assert "ExecStart=/opt/lumiverb/.venv/bin/python -m src.server.scheduler" in unit
    assert f"Environment=XDG_CACHE_HOME={data}/cache" in unit
    assert f"Environment=CUDA_CACHE_PATH={data}/cache/nvidia" in unit
    assert f"Environment=TMPDIR={data}/worker-tmp" in unit
    assert f"ExecStartPre=-/usr/bin/find {data}/worker-tmp -mindepth 1 -delete" in unit
    # Its home is read-only to it: nothing it runs writes there (matplotlib's settings and CUDA's kernels are on
    # the data disk), and it's where git, npm and uv keep the settings root's update would otherwise follow.
    assert f"ReadWritePaths={data}\n" in unit and "Environment=HOME=/var/lib/lumiverb" in unit
    assert [c for c in calls if c.startswith("chown")] == [
        f"chown -h lumiverb:lumiverb -- {data}/worker-tmp", f"chown -h lumiverb:lumiverb -- {data}/cache"]
    assert "After=network-online.target remote-fs.target lumiverb-api.service" in unit
    # Only the scheduler hears a stop: ffmpeg under a job isn't killed mid-clip.
    assert "KillMode=mixed" in unit
    # The worker goes: the two never run together. Its stop reaches only its
    # main process (review round 2): the old code, killed with its ffmpeg,
    # would save an empty transcript one last time.
    assert "disable --now lumiverb-worker [worker: KillMode=mixed]" in calls
    assert calls.index("daemon-reload [worker: KillMode=mixed]") < calls.index(
        "disable --now lumiverb-worker [worker: KillMode=mixed]")
    assert not (units / "lumiverb-worker.service").exists()
    assert not (units / "lumiverb-worker.service.d").exists()
    assert "enable lumiverb-scheduler" in calls
    assert (data / "worker-tmp").is_dir() and (data / "cache").is_dir()


def test_rerunning_the_update_writes_the_same_scheduler(tmp_path):
    data = tmp_path / "data"
    _, _, units = _update_scheduler(tmp_path, f"DATA_DIR={data}\n")
    first = (units / "lumiverb-scheduler.service").read_text()
    _, calls, _ = _update_scheduler(tmp_path, f"DATA_DIR={data}\n", worker=False)
    assert (units / "lumiverb-scheduler.service").read_text() == first
    assert not any("lumiverb-worker" in c for c in calls)


def test_a_link_where_a_data_dir_folder_goes_stops_the_update_before_root_follows_it(tmp_path):
    # The service user owns DATA_DIR and could put a link there: root's mkdir and chown would follow it.
    data, elsewhere = tmp_path / "data", tmp_path / "etc"
    data.mkdir()
    elsewhere.mkdir()
    (data / "cache").symlink_to(elsewhere)
    out, calls, units = _update_scheduler(tmp_path, f"DATA_DIR={data}\n", worker=False)
    assert (f"fail: {data}/cache is a symbolic link where Lumiverb keeps a folder: remove the link "
            f"(sudo rm {data}/cache), then run this again") in out
    assert not [c for c in calls if c.startswith("chown")], "nothing's owner changed"
    assert not (units / "lumiverb-scheduler.service").exists()
    # A dangling one too: mkdir -p would make what it names.
    (data / "cache").unlink()
    (data / "cache").symlink_to(tmp_path / "made-by-root")
    out, calls, _ = _update_scheduler(tmp_path, f"DATA_DIR={data}\n", worker=False)
    assert "fail: " in out and not (tmp_path / "made-by-root").exists()
    for step, folder in (("plug-ins", "plugins"), ("data dir", "tmp")):
        (data / folder).symlink_to(elsewhere)
        out = (_update_plugins(tmp_path, f"DATA_DIR={data}\n")[0] if step == "plug-ins"
               else _run_update_data_dir(tmp_path, f"DATA_DIR={data}\n").stdout)
        assert f"fail: {data}/{folder} is a symbolic link" in out, step
        (data / folder).unlink()


@pytest.mark.parametrize(("env_text", "said"), [
    ("DATA_DIR=/mnt/a\nDATA_DIR=/mnt/b\n", "fail: DATA_DIR is set 2 times in"),
    ("DATA_DIR=mnt/a\n", "fail: DATA_DIR must be one full path"),
    ("DATA_DIR=/mnt/a b\n", "fail: DATA_DIR must be one full path"),
    ("DATA_DIR=/mnt/a\r\n", "fail: DATA_DIR must be one full path"),
    ("DATA_DIR=/mnt/a%h\n", "fail: DATA_DIR must be one full path"),
    ("DATA_DIR=/mnt/$ADMIN_KEY\n", "fail: DATA_DIR must be one full path"),
], ids=["twice", "relative", "space", "carriage-return", "specifier", "variable"])
def test_a_data_dir_that_isnt_one_full_path_stops_the_update_before_a_unit_is_written(tmp_path, env_text, said):
    # It's written into units and acted on as root: a second line would be read as more of the unit.
    out, calls, units = _update_scheduler(tmp_path, env_text, worker=False)
    assert said in out
    assert not (units / "lumiverb-scheduler.service").exists() and calls == []
    out, calls, units = _update_plugins(tmp_path, env_text)
    assert said in out and not (units / "lumiverb-plugins.service").exists()
    assert "\n" not in out.split("fail: ", 1)[1].strip(), "said on one line, whatever it holds"


def test_a_data_dir_flag_that_isnt_one_full_path_is_refused_before_anything(tmp_path):
    for bad in ("data", "/mnt/a b", "/mnt/a\nExecStartPre=+/bin/true"):
        out = subprocess.run(["bash", str(DEPLOY_API), "--app-host", "http://x", "--data-dir", bad, "--dry-run"],
                             capture_output=True, text=True, timeout=30, check=False,
                             env={**os.environ, "LUMIVERB_CONF_DIR": str(tmp_path / "conf")})
        assert out.returncode != 0 and "DATA_DIR must be one full path" in out.stderr, bad
        assert "DATA_DIR=" not in out.stdout


def test_install_and_update_hold_data_dir_folders_and_paths_the_same_way():
    for name in ("svc_folder", "data_dir_shape"):
        assert _function(DEPLOY_API.read_text(), name) == _function(UPDATE_API.read_text(), name), name
    # Every folder root makes in DATA_DIR for the service user goes through it: no chown follows a link.
    for script in (DEPLOY_API, UPDATE_API):
        for line in script.read_text().splitlines():
            if line.lstrip().startswith("chown ") and '"$DATA_DIR"' in line:
                assert line.lstrip().startswith(('chown -h ', 'chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"')), line
        text = script.read_text()
        assert 'mkdir -p "$DATA_DIR"/' not in text and "mkdir -p \"${DATA_DIR}\"/" not in text
    # Every DATA_DIR an update reads is one line, checked.
    assert "grep '^DATA_DIR=' " not in UPDATE_API.read_text()
    assert UPDATE_API.read_text().count("read_data_dir\n") >= 5


def test_the_scheduler_needs_the_data_disk(tmp_path):
    # XDG_CACHE_HOME=/cache would be read-only under ProtectSystem=strict:
    # the scheduler couldn't make its lock, and wouldn't start.
    out, calls, units = _update_scheduler(tmp_path, "API_PORT=8100\n")
    assert "fail: No DATA_DIR" in out
    assert not (units / "lumiverb-scheduler.service").exists()
    assert (units / "lumiverb-worker.service").exists()


def test_a_machine_without_processing_gets_no_scheduler(tmp_path):
    out, calls, units = _update_scheduler(tmp_path, f"DATA_DIR={tmp_path}/data\n", processing=False, worker=False)
    assert not (units / "lumiverb-scheduler.service").exists()
    assert calls == []


def test_processing_keeps_its_packages_whichever_service_it_has():
    text = UPDATE_API.read_text()
    deps = text.split('step "Updating Python dependencies"', 1)[1].split("# ----", 1)[0]
    assert "is-enabled lumiverb-scheduler" in deps and "is-enabled lumiverb-worker" in deps
    assert "--extra workers" in deps
    # A missing data dir stops the update before anything changes (not after migrating).
    assert deps.index("No DATA_DIR") < text.index('step "Running migrations"') - text.index('step "Updating Python dependencies"')


def _update_plugins(tmp_path: Path, env_text: str) -> tuple[str, list[str], Path]:
    """Run update-api.sh's plug-in runner step; returns its output, the systemctl calls and the units' folder."""
    env = tmp_path / "env"
    env.write_text(env_text)
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    calls = tmp_path / "systemctl-calls"
    calls.write_text("")
    text = UPDATE_API.read_text()
    block = text.split('step "Installing the plug-in runner"', 1)[1].split("\n", 1)[1].split("# ----", 1)[0]
    script = (
        'step() { :; }; ok() { echo "ok: $1"; }; warn() { echo "warn: $1"; }; chown() { :; }\n'
        'fail() { echo "fail: $1"; exit 1; }\n'
        f'systemctl() {{ echo "$*" >> "{calls}"; }}\n'
        f'ENV_FILE="{env}"; APP_DIR=/opt/lumiverb; SVC_HOME=/var/lib/lumiverb; SVC_USER=lumiverb\n'
        + _update_helpers() + block.replace("/etc/systemd/system/", f"{units}/")
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    return out.stdout + out.stderr, calls.read_text().splitlines(), units


def test_update_installs_the_plug_in_runner(tmp_path):
    # ADR-023: every installed plug-in, a process of its own, writing only its folder.
    data = tmp_path / "data"
    out, calls, units = _update_plugins(tmp_path, f"DATA_DIR={data}\n")
    unit = (units / "lumiverb-plugins.service").read_text()
    assert "ExecStart=/opt/lumiverb/.venv/bin/python -m src.server.plugin_runner" in unit
    assert f"ReadWritePaths={data}/plugins" in unit and "ProtectSystem=strict" in unit
    assert "MemoryMax=4G" in unit and "KillMode=mixed" in unit and "Restart=always" in unit
    assert calls == ["daemon-reload", "enable lumiverb-plugins"]
    assert (data / "plugins").is_dir()
    # Rerun: the same unit.
    _update_plugins(tmp_path, f"DATA_DIR={data}\n")
    assert (units / "lumiverb-plugins.service").read_text() == unit


def test_without_a_data_disk_there_is_no_plug_in_runner(tmp_path):
    out, calls, units = _update_plugins(tmp_path, "API_PORT=8100\n")
    assert "warn: No DATA_DIR" in out and calls == []
    assert not (units / "lumiverb-plugins.service").exists()


def _function(text: str, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"no {name}()"
    return m.group(0)


def test_install_and_update_bring_the_same_heic_decoder():
    assert _function(DEPLOY_API.read_text(), "heic_decoder") == _function(UPDATE_API.read_text(), "heic_decoder")
    worker = DEPLOY_API.read_text().split('if [[ "$WITH_WORKER" == "true" ]]; then\n  # Scan and enrich', 1)[1]
    worker = worker.split("\nfi\n", 1)[0]
    assert worker.index("libvips42") < worker.index("heic_decoder"), "with libvips, where processing is"


def _photo_decoders(tmp_path: Path, *, processing: bool, installed: bool = False, known: bool = True,
                    installs: bool = True, vips: str | None = None) -> tuple[int, str, list[str]]:
    """Run update-api.sh's photo decoders step with dpkg, apt-cache and apt-get stubbed: whether the package is
    installed, whether the distribution has it, whether installing it works, and the libvips installed (vips, its
    package's version; None: none). Versions are compared by the real dpkg. Returns its exit code, its output and
    the apt-get calls."""
    calls = tmp_path / "apt-calls"
    calls.write_text("")
    text = UPDATE_API.read_text()
    block = text.split('step "Photo decoders"', 1)[1].split("\n", 1)[1].split("# ----", 1)[0]
    script = (
        'ok() { echo "ok: $1"; }; warn() { echo "warn: $1"; }\n'
        'dpkg() { if [[ "$1" == --compare-versions ]]; then command dpkg "$@"; else '
        f'{"return 0" if installed else "return 1"}; fi; }}\n'
        f'dpkg-query() {{ {f"echo ii {vips}" if vips else "return 1"}; }}\n'
        f'apt-cache() {{ {"return 0" if known else "return 100"}; }}\n'
        f'apt-get() {{ echo "$*" >> "{calls}"; [[ "$1" == update ]] || {"return 0" if installs else "return 100"}; }}\n'
        f'PROCESSING={"true" if processing else "false"}\n' + block
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    return out.returncode, out.stdout + out.stderr, calls.read_text().splitlines()


def test_an_update_brings_the_heic_decoder_where_the_distribution_has_it(tmp_path):
    rc, out, apt = _photo_decoders(tmp_path, processing=True)
    assert rc == 0 and "ok: HEIC photos: installed libheif-plugin-libde265" in out, out
    assert apt == ["install -y -qq libheif-plugin-libde265"]


def test_an_update_leaves_it_be_where_its_there_or_not_a_package(tmp_path):
    rc, out, apt = _photo_decoders(tmp_path, processing=True, installed=True)
    assert rc == 0 and "is installed" in out and apt == []
    rc, out, apt = _photo_decoders(tmp_path, processing=True, known=False)  # Ubuntu 22.04: libheif decodes them
    assert rc == 0 and "libheif decodes them itself here" in out and apt == []
    rc, out, apt = _photo_decoders(tmp_path, processing=False)
    assert rc == 0 and "nothing to decode" in out and apt == []


def test_an_update_whose_decoder_wont_install_says_so_and_goes_on(tmp_path):
    rc, out, apt = _photo_decoders(tmp_path, processing=True, installs=False)
    assert rc == 0, "every other photo is read as before: never a failed update"
    assert "warn: HEIC photos can't be read until libheif-plugin-libde265 is installed" in out
    assert apt == ["install -y -qq libheif-plugin-libde265", "update -qq",
                   "install -y -qq libheif-plugin-libde265"], "the lists made fresh once, then tried again"


# -- libvips: 8.13 or later (src/processing/libvips.py refuses an older one) -----------------------------------

LIBVIPS_RULE = re.compile(r"^# Photos are read with libvips .*?^libvips_too_old\(\) \{\n.*?^\}\n", re.S | re.M)


def test_install_and_update_hold_libvips_to_what_the_code_needs():
    deploy, update = (LIBVIPS_RULE.search(s.read_text()) for s in (DEPLOY_API, UPDATE_API))
    assert deploy and update and deploy.group(0) == update.group(0), "the same text in both"
    code = (REPO / "src" / "processing" / "libvips.py").read_text()
    needed = re.search(r"^TRUSTED_ONLY_SINCE = \((\d+), (\d+)\)", code, re.M)
    assert f"\nLIBVIPS_NEEDED={needed.group(1)}.{needed.group(2)}\n" in deploy.group(0)


def _libvips_at_install(tmp_path: Path, candidate: str | None) -> tuple[int, str]:
    """deploy-api.sh's libvips_check, with apt-cache saying what libvips42 it would install (None: none)."""
    text = DEPLOY_API.read_text()
    policy = (f"libvips42t64:\n  Installed: (none)\n  Candidate: (none)\nlibvips42:\n  Installed: (none)\n"
              f"  Candidate: {candidate}\n" if candidate else "")
    script = (
        'fail() { echo "fail: $1"; exit 1; }\n'
        f"apt-cache() {{ printf '%s' {shlex.quote(policy)}; }}\n"
        + LIBVIPS_RULE.search(text).group(0) + _function(text, "libvips_check") + "libvips_check\necho went on\n"
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    return out.returncode, out.stdout + out.stderr


def test_an_install_with_processing_stops_where_libvips_is_too_old(tmp_path):
    rc, out = _libvips_at_install(tmp_path, "8.12.1-1build1")  # Ubuntu 22.04's
    assert rc == 1 and "went on" not in out, out
    assert ("fail: This system's libvips is 8.12.1: Lumiverb reads photos with libvips 8.13 or later, the first that "
            "can turn off its untrusted loaders. Install on Ubuntu 24.04 or later") in out
    for candidate in ("8.15.1-1.1build4", "8.14.1-3+deb12u3", "8.13.0-1", None):  # 24.04's, Debian 12's
        rc, out = _libvips_at_install(tmp_path, candidate)
        assert rc == 0 and out.strip() == "went on", (candidate, out)
    text = DEPLOY_API.read_text()
    packages = text.split('step "Installing system packages"', 1)[1]
    assert packages.index("libvips_check") < packages.index("apt-get install"), "before any package is installed"
    assert '[[ "$WITH_WORKER" != "true" ]] || libvips_check' in packages, "only where processing is"


def test_an_update_says_a_libvips_too_old_and_goes_on(tmp_path):
    rc, out, _ = _photo_decoders(tmp_path, processing=True, installed=True, vips="8.12.1-1build1")
    assert rc == 0, "the rest works as before: never a failed update"
    assert ("warn: This system's libvips is 8.12.1: Lumiverb reads photos with libvips 8.13 or later, the first "
            "that can turn off its untrusted loaders, so scans stop until it's newer") in out
    rc, out, _ = _photo_decoders(tmp_path, processing=True, installed=True, vips="8.15.1-1.1build4")
    assert rc == 0 and "ok: libvips 8.15.1" in out and "warn:" not in out, out
    rc, out, _ = _photo_decoders(tmp_path, processing=True, installed=True)  # none from a package: nothing to say
    assert rc == 0 and "libvips" not in out, out
    rc, out, _ = _photo_decoders(tmp_path, processing=False, vips="8.12.1-1build1")
    assert rc == 0 and "libvips" not in out, "no processing here: nothing reads photos"


def test_a_fresh_install_and_an_update_write_the_same_plug_in_runner():
    def unit(text: str) -> list[str]:
        body = text.split("Description=Lumiverb plug-ins", 1)[1].split("\nUNIT", 1)[0]
        return [line.strip() for line in body.splitlines()]

    assert unit(DEPLOY_API.read_text()) == unit(UPDATE_API.read_text())
    deploy = DEPLOY_API.read_text()
    assert "systemctl enable --now lumiverb-api lumiverb-plugins" in deploy
    assert deploy.index('svc_folder "$DATA_DIR"/plugins') < deploy.index("lumiverb-plugins.service <<UNIT")


def test_the_plug_in_runner_stops_for_the_migrations_and_starts_once_the_api_answers():
    text = UPDATE_API.read_text()
    migrations = text.split('step "Running migrations"', 1)[1].split("# ----", 1)[0]
    assert "systemctl stop lumiverb-plugins" in migrations
    restart = text.split('step "Restarting services"', 1)[1]
    assert restart.index("curl -sf http://127.0.0.1:${API_PORT}/health") < restart.index(
        "systemctl restart lumiverb-plugins") < restart.index("systemctl start lumiverb-scheduler")


def test_scheduler_temp_files_go_on_the_data_disk():
    # Whisper's WAVs (hundreds of MB for a long clip) went to PrivateTmp's
    # /tmp, RAM on prod. Not the API's tmp: each API start empties that.
    text = DEPLOY_API.read_text()
    unit = text.split("Description=Lumiverb scheduler", 1)[1].split("UNIT", 1)[0]
    assert "Environment=TMPDIR=${DATA_DIR}/worker-tmp" in unit
    assert "ExecStartPre=-/usr/bin/find ${DATA_DIR}/worker-tmp -mindepth 1 -delete" in unit
    assert '"$DATA_DIR"/worker-tmp' in text.split('step "Creating service user and directories"', 1)[1].split("ok ", 1)[0]


def test_a_fresh_install_starts_the_scheduler_not_the_worker():
    text = DEPLOY_API.read_text()
    start = text.split("# 13. Scheduler", 1)[1]
    assert "systemctl enable lumiverb-scheduler" in start
    assert "systemctl disable --now lumiverb-worker" in start
    assert "systemctl enable lumiverb-worker" not in text
    # Only the old worker's main process hears its stop (as in update-api.sh).
    assert start.index("KillMode=mixed") < start.index("systemctl daemon-reload") < start.index(
        "systemctl disable --now lumiverb-worker")


def test_the_scheduler_s_caches_are_on_the_data_disk_and_the_cli_keeps_none():
    # The scheduler's caches, lock and state follow XDG_CACHE_HOME (its env
    # file). The service user's CLI isn't pointed at them: a client keeps
    # no caches (ADR-020 decision 7), so it never shares the scheduler's.
    text = DEPLOY_API.read_text()
    env_block = text.split('cat > "$ENV_FILE" <<ENVEOF', 1)[1].split("ENVEOF", 1)[0]
    assert "\nXDG_CACHE_HOME=${DATA_DIR}/cache\n" in env_block
    assert "--cache-home" not in text


def _env_append() -> str:
    """update-api.sh's helper for adding a line to the env file."""
    text = UPDATE_API.read_text()
    start = text.index("env_append() {")
    return text[start:text.index("\n}\n", start) + 3]


def _run_update_data_dir(tmp_path: Path, env_text: str) -> subprocess.CompletedProcess:
    """Run update-api.sh's data-dir step against an env file (its sudo calls noted in sudo-calls)."""
    env = tmp_path / "env"
    env.write_text(env_text)
    calls = tmp_path / "sudo-calls"
    calls.touch()
    text = UPDATE_API.read_text()
    block = text.split('step "Ensuring data directory"', 1)[1].split("# ----", 1)[0]
    script = (
        'step() { :; }; ok() { :; }; warn() { :; }; chown() { :; }; fail() { echo "fail: $1"; exit 1; }\n'
        f'sudo() {{ echo "$*" >> "{calls}"; }}\n'
        f'ENV_FILE="{env}"; SVC_USER=lumiverb; APP_DIR=/opt/lumiverb\n'
        + _env_append() + _update_helpers() + block
    )
    return subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)


def _update_data_dir(tmp_path: Path, env_text: str) -> tuple[str, list[str]]:
    """Run update-api.sh's data-dir step against an env file; returns it and the sudo calls."""
    out = _run_update_data_dir(tmp_path, env_text)
    assert out.returncode == 0, out.stdout + out.stderr
    return (tmp_path / "env").read_text(), (tmp_path / "sudo-calls").read_text().splitlines()


def test_update_points_the_scheduler_s_caches_at_the_data_disk(tmp_path):
    data = tmp_path / "data"
    _update_data_dir(tmp_path, f"DATA_DIR={data}\nAPI_PORT=8100\n")
    env, calls = _update_data_dir(tmp_path, (tmp_path / "env").read_text())  # reruns add nothing
    assert env.splitlines().count(f"XDG_CACHE_HOME={data}/cache") == 1
    assert calls == [], "the service user's CLI isn't pointed at them: it keeps no caches"


def test_update_without_a_data_dir_leaves_caches_alone(tmp_path):
    env, calls = _update_data_dir(tmp_path, "API_PORT=8100\n")
    assert env == "API_PORT=8100\n"
    assert calls == []


def test_settings_added_to_an_env_file_without_a_final_newline_get_their_own_line(tmp_path):
    # An env file edited by hand often ends without one: "API_PORT=8100XDG_CACHE_HOME=..."
    # would break both settings.
    data = tmp_path / "data"
    env, _ = _update_data_dir(tmp_path, f"DATA_DIR={data}\nAPI_PORT=8100")
    assert env.splitlines() == [f"DATA_DIR={data}", "API_PORT=8100", f"XDG_CACHE_HOME={data}/cache"]

    env_file = tmp_path / "env"
    env_file.write_text(f"DATA_DIR={data}")
    text = UPDATE_API.read_text()
    block = text.split('step "Ports"', 1)[1].split("# ----", 1)[0]
    script = 'step() { :; }; ok() { :; }\n' + f'ENV_FILE="{env_file}"\n' + _env_append() + block
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert env_file.read_text().splitlines() == [f"DATA_DIR={data}", "API_PORT=8000"]


def test_every_env_file_append_goes_through_the_helper():
    text = UPDATE_API.read_text()
    assert text.replace(_env_append(), "").count('>> "$ENV_FILE"') == 0


def _update_python(tmp_path: Path, venv_cfg: str | None, pin: str = "3.12\n", fail_on: str = "",
                   ok: bool = True) -> tuple[list[str], str, str]:
    """Run update-api.sh's dependencies step with systemctl and sudo stubbed.

    Returns the calls in order, stdout and stderr. `fail_on`: a sudo call
    containing this fails.
    """
    app = tmp_path / "app"
    (app / ".venv").mkdir(parents=True, exist_ok=True)
    (app / ".venv-next").mkdir(exist_ok=True)  # what the side build leaves
    (app / ".python-version").write_text(pin)
    if venv_cfg is not None:
        (app / ".venv" / "pyvenv.cfg").write_text(venv_cfg)
    calls = tmp_path / "calls"
    calls.write_text("")
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    env_file = tmp_path / "env"
    env_file.write_text("DATA_DIR=/mnt/ssd2/lumiverb\n")
    text = UPDATE_API.read_text()
    block = text.split('step "Updating Python dependencies"', 1)[1].split("# ----", 1)[0]
    script = (
        "RED=''; NC=''\n"
        'step() { :; }; ok() { :; }; warn() { echo "warn: $1"; }; fail() { echo "fail: $1" >&2; exit 1; }\n'
        # systemctl remembers what it stopped, so is-active can say so.
        f'systemctl() {{ echo "systemctl $*" >> "{calls}"; '
        f'if [[ "$1" == stop ]]; then touch "{tmp_path}/stopped"; fi; '
        f'if [[ "$1" == is-active ]]; then [[ ! -e "{tmp_path}/stopped" ]]; fi; }}\n'
        # What it reads as the service user (sed, grep) is read, as us; the rest is noted.
        f'sudo() {{ echo "sudo $*" >> "{calls}"; [[ -z "{fail_on}" || "$*" != *"{fail_on}"* ]] || return 1; '
        'shift 2; if [[ "$1" == env ]]; then shift; while [[ "$1" == *=* ]]; do shift; done; fi; '
        'case "$1" in sed|grep) "$@" ;; esac; }\n'
        f'APP_DIR="{app}"; SVC_USER=lumiverb; UV_BIN=/usr/local/bin/uv; ENV_FILE="{env_file}"\n'
        # Never this machine's own units (prod's may be here).
        + _update_helpers() + block.replace("/etc/systemd/system/", f"{units}/")
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert (out.returncode == 0) == ok, out.stdout + out.stderr
    return calls.read_text().splitlines(), out.stdout, out.stderr


VENV_CFG = "home = /usr/bin\nimplementation = CPython\nuv = 0.7.12\nversion_info = {}\nprompt = lumiverb\n"


def test_a_new_python_is_built_beside_the_running_one_then_swapped_in(tmp_path):
    # uv sync deletes the 3.14 venv and downloads GBs of 3.12 wheels: do the
    # downloading into a side venv while the API and scheduler run, then stop
    # them only for the quick swap from the warm cache.
    calls, out, _ = _update_python(tmp_path, VENV_CFG.format("3.14.4"))
    assert "Python changes from 3.14 to 3.12" in out
    install = next(i for i, c in enumerate(calls) if "python install 3.12" in c)
    side = next(i for i, c in enumerate(calls) if "UV_PROJECT_ENVIRONMENT=" in c and ".venv-next" in c)
    stop_worker = calls.index("systemctl stop lumiverb-scheduler")
    stop_api = calls.index("systemctl stop lumiverb-api")
    swap = max(i for i, c in enumerate(calls) if " sync " in c and "UV_PROJECT_ENVIRONMENT" not in c)
    assert install < side < stop_worker < swap and side < stop_api < swap
    assert not (tmp_path / "app" / ".venv-next").exists()


@pytest.mark.parametrize("cfg", [VENV_CFG.format("3.12.11"), None], ids=["same-python", "no-venv-yet"])
def test_update_on_the_same_python_keeps_the_services_running(tmp_path, cfg):
    calls, out, _ = _update_python(tmp_path, cfg)
    assert not [c for c in calls if c.startswith("systemctl stop")]
    assert not [c for c in calls if "python install" in c or "UV_PROJECT_ENVIRONMENT" in c]
    assert "Python changes" not in out
    assert any(" sync " in c for c in calls)


@pytest.mark.parametrize("step", ["python install", "UV_PROJECT_ENVIRONMENT"])
def test_a_failed_side_build_leaves_lumiverb_running(tmp_path, step):
    calls, _, err = _update_python(tmp_path, VENV_CFG.format("3.14.4"), fail_on=step, ok=False)
    assert not [c for c in calls if c.startswith("systemctl stop")]
    assert "stopped" not in err


def test_a_half_made_side_venv_is_cleared_before_building(tmp_path):
    # uv refuses a non-empty folder that isn't a venv (an interrupted run).
    (tmp_path / "app" / ".venv-next").mkdir(parents=True)
    (tmp_path / "app" / ".venv-next" / "junk").write_text("x")
    _update_python(tmp_path, VENV_CFG.format("3.14.4"), fail_on="UV_PROJECT_ENVIRONMENT", ok=False)
    assert not (tmp_path / "app" / ".venv-next" / "junk").exists()


def test_a_failure_while_stopped_says_how_to_recover(tmp_path):
    # The swap fails after the services were stopped: say so plainly.
    calls, _, err = _update_python(tmp_path, VENV_CFG.format("3.14.4"), fail_on="UV_NO_CONFIG=1 /usr/local/bin/uv sync", ok=False)
    assert "systemctl stop lumiverb-api" in calls
    assert "isn't running" in err and "update.sh" in err


def test_a_rerun_that_fails_while_still_stopped_says_so_too(tmp_path):
    # Python already swapped (3.12 = 3.12), the API still down from last time.
    (tmp_path / "stopped").write_text("")
    _, _, err = _update_python(tmp_path, VENV_CFG.format("3.12.15"), fail_on="UV_NO_CONFIG=1 /usr/local/bin/uv sync", ok=False)
    assert "isn't running" in err


def test_every_update_cleans_uvs_cache_once_running():
    # uv cache prune keeps an old Python's packages (still "used"); clean
    # frees them, and the venv's own files don't depend on the cache. Every
    # update, so a rerun after a failure still gets there.
    text = UPDATE_API.read_text()
    restart = text.split('step "Restarting services"', 1)[1]
    clean = restart.index("cache clean")
    assert restart.index("systemctl restart lumiverb-api") < clean
    assert "PY_CHANGED" not in restart[clean - 200:clean]
    assert "cache prune" not in text


def test_the_side_venv_isnt_tracked():
    assert ".venv-next/" in (REPO / ".gitignore").read_text().splitlines()


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
    assert s["BRANCH"] == "feat/other"
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
    ("lumiverb-worker", (), "true"), ("lumiverb-scheduler", (), "true"), ("", (), "false"),
    ("", ("--worker",), "true"),
])
def test_a_dry_run_reports_processing_as_a_real_run_decides(tmp_path, enabled, flag, worker):
    # A real run keeps installed processing (the scheduler, or the worker it
    # replaces) without --worker.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text("#!/bin/sh\n" + (f'[ "$1 $2" = "is-enabled {enabled}" ] && exit 0\n' if enabled else "")
                         + "exit 1\n")
    systemctl.chmod(0o755)
    assert _settings(tmp_path, *flag, env_file=REMEMBERED, path_prefix=bin_dir)["WORKER"] == worker


def test_the_real_run_uses_the_worker_setting_the_dry_run_shows():
    text = DEPLOY_API.read_text()
    deps = text.split('step "Installing Python dependencies"', 1)[1].split("# ----", 1)[0]
    assert "is-enabled" not in deps
    assert text.index("systemctl is-enabled lumiverb-worker") < text.index('if [[ "$DRY_RUN" == "true" ]]')
    assert text.index("systemctl is-enabled lumiverb-scheduler") < text.index('if [[ "$DRY_RUN" == "true" ]]')


def test_web_on_its_own_machine_uses_defaults_and_flags(tmp_path):
    assert _web_settings(tmp_path)["BRANCH"] == "main"
    assert _web_settings(tmp_path)["NO_FIREWALL"] == "false"
    s = _web_settings(tmp_path, "--branch", "x", "--no-firewall", env_file=REMEMBERED.replace("BRANCH=feat/other", "BRANCH=y"))
    assert s["BRANCH"] == "x"
    assert s["NO_FIREWALL"] == "true"


def _web_dry_run(tmp_path: Path, domain: str, upstream: str) -> subprocess.CompletedProcess:
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(DEPLOY_WEB), "--domain", domain, "--api-upstream", upstream, "--dry-run"],
        capture_output=True, text=True, env={**os.environ, "LUMIVERB_CONF_DIR": str(conf)}, timeout=30, check=False,
    )


@pytest.mark.parametrize(("domain", "upstream"), [
    ("_", "http://127.0.0.1:8100"), ("app.lumiverb.io", "http://10.0.0.5:8000/"), ("lumiverb.local", "https://api:8443"),
    ("_", "http://[::1]:8100"), ("photos.home.arpa", "http://localhost:8000"),
])
def test_web_takes_a_domain_and_the_api_s_address(tmp_path, domain, upstream):
    out = _web_dry_run(tmp_path, domain, upstream)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"DOMAIN={domain}\n" in out.stdout and f"API_UPSTREAM={upstream}\n" in out.stdout


@pytest.mark.parametrize(("domain", "upstream", "said"), [
    ("app.lumiverb.io;", "http://127.0.0.1:8100", "Not a domain name"),
    ("a.io; location / { return 200; }", "http://127.0.0.1:8100", "Not a domain name"),
    ("app.lumiverb.io\n", "http://127.0.0.1:8100", "Not a domain name"),
    ("$host", "http://127.0.0.1:8100", "Not a domain name"),
    ("_", "http://127.0.0.1:8100; }", "Not the API's address"),
    ("_", "http://127.0.0.1:8100/v1/x", "Not the API's address"),
    ("_", "ftp://127.0.0.1:8100", "Not the API's address"),
    ("_", "http://$host:8100", "Not the API's address"),
    ("_", "127.0.0.1:8100", "Not the API's address"),
])
def test_web_refuses_anything_else_before_writing_nginx_s_config(tmp_path, domain, upstream, said):
    # Each is written into nginx's config as typed: a value that's more than a name or an address is refused,
    # as deploy-site.sh refuses its names, before anything is installed or written.
    out = _web_dry_run(tmp_path, domain, upstream)
    assert out.returncode != 0 and said in out.stderr, out.stdout + out.stderr
    assert "DOMAIN=" not in out.stdout


def _deploy_web_site(tmp_path: Path, upstream: str = "http://127.0.0.1:8100") -> str:
    """The nginx site deploy-web.sh writes for --domain _."""
    text = DEPLOY_WEB.read_text()
    start = text.index("cat > /etc/nginx/sites-available/lumiverb <<NGINX")
    end = text.index("\nNGINX\n", start) + len("\nNGINX\n")
    site = tmp_path / "fresh-site"
    csp = next(line for line in text.splitlines() if line.startswith("CSP="))
    script = (f'API_UPSTREAM={upstream}; LISTEN="listen 80 default_server;"; DOMAIN=_; APP_DIR=/opt/lumiverb\n{csp}\n'
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
    script = (f'step() {{ :; }}; ok() {{ :; }}; warn() {{ :; }}; APP_DIR={REPO}; LUMIVERB_NGINX_CONF_D={site.parent}; export LUMIVERB_SITE_LOG_DIR={site.parent}/log LUMIVERB_LOGROTATE_D={site.parent}\n'
              + block.replace("/etc/nginx/sites-available/lumiverb", str(site)))
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr


def test_update_gives_an_existing_site_the_playback_location_once(tmp_path):
    site = tmp_path / "lumiverb"
    site.write_text(OLD_SITE)
    _update_web_site(site)
    _update_web_site(site)  # reruns change nothing
    assert site.read_text() == _deploy_web_site(tmp_path)


def _csp(script: Path) -> str:
    return next(line for line in script.read_text().splitlines() if line.startswith("CSP="))


def test_every_response_of_the_page_has_the_csp(tmp_path):
    # A location's add_header drops the server's: the page and the built
    # files carry it too. Same policy from deploy and update.
    site = _deploy_web_site(tmp_path)
    assert _csp(DEPLOY_WEB) == _csp(UPDATE_WEB)
    assert "unsafe" not in _csp(DEPLOY_WEB)
    for block in (site.split("    location ", 1)[0], *(_location(site, p) for p in ("/assets/", "= /index.html"))):
        assert "Content-Security-Policy" in str(block)


def _certbot(site: str) -> str:
    """What certbot --nginx --redirect makes of the site: 443 in the block, a port-80 redirect after it."""
    site = site.replace("    listen 80 default_server;\n", "", 1)
    head, tail = site.rsplit("}\n", 1)
    return (head + "    listen 443 ssl; # managed by Certbot\n    listen [::]:443 ssl; # managed by Certbot\n}\n" + tail
            + "\nserver {\n    if ($host = x) {\n        return 301 https://$host$request_uri;\n    }\n"
            "    listen 80;\n    server_name x;\n    return 404; # managed by Certbot\n}\n")


def _add_hsts(site: Path) -> None:
    text = DEPLOY_WEB.read_text()
    fn = text[text.index("add_hsts() {"):text.index("\n}\n", text.index("add_hsts() {")) + 3]
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", f"{fn}\nadd_hsts {site}\nadd_hsts {site}"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


def _site_update(site: Path, *args: str) -> None:
    """What deploy-web.sh (and deploy-site.sh, --static) run once certbot has written its HTTPS."""
    out = subprocess.run([sys.executable, "-I", str(REPO / "src/server/lifecycle/site_update.py"), *args, str(site)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


def test_hsts_goes_on_https_blocks_only(tmp_path):
    https = tmp_path / "https"
    https.write_text(_certbot(_deploy_web_site(tmp_path)))
    _add_hsts(https)
    tls, redirect = https.read_text().split("\nserver {", 1)
    assert tls.count('add_header Strict-Transport-Security "max-age=31536000" always;') == 3
    assert "Strict-Transport-Security" not in redirect

    http = tmp_path / "http"
    http.write_text(_deploy_web_site(tmp_path))
    _add_hsts(http)
    assert "Strict-Transport-Security" not in http.read_text()


def test_update_gives_an_https_site_what_deploy_does(tmp_path):
    site = tmp_path / "lumiverb"
    site.write_text(_certbot(OLD_SITE))
    _update_web_site(site)
    _update_web_site(site)
    deployed = tmp_path / "deployed"
    deployed.write_text(_certbot(_deploy_web_site(tmp_path)))
    _add_hsts(deployed)
    _site_update(deployed)
    assert site.read_text() == deployed.read_text()


def _site_v0_1_160() -> str:
    """The site deploy-web.sh wrote at v0.1.160: a Mac's config of then (init.py's, the same server block) as
    Linux's."""
    mac = (REPO / "tests" / "fixtures" / "nginx-mac-v0.1.160.conf").read_text()
    block = mac[mac.index("    server {"):mac.rindex("    }\n") + len("    }\n")]
    site = "".join(line[4:] + "\n" for line in block.splitlines())
    return (site.replace("include /usr/local/var/lumiverb/nginx/listen.conf;", "listen 80 default_server;")
            .replace("/opt/homebrew/opt/lumiverb/libexec/", "/opt/lumiverb/")
            .replace("http://127.0.0.1:6364", "http://127.0.0.1:8100"))


@pytest.mark.parametrize("https", [False, True])
def test_update_gives_a_site_from_v0_1_160_what_deploy_does_now(tmp_path, https):
    # Video from blob: URLs, the API's referrer and permissions policies, compression, 404s for probes
    # (src/server/lifecycle/site_update.py, which update-web.sh runs).
    site = tmp_path / "lumiverb"
    old = _site_v0_1_160()
    assert "media-src 'self';" in old and "gzip" not in old
    site.write_text(_certbot(old) if https else old)
    if https:
        _add_hsts(site)
    _update_web_site(site)
    _update_web_site(site)
    deployed = tmp_path / "deployed"
    deployed.write_text(_certbot(_deploy_web_site(tmp_path)) if https else _deploy_web_site(tmp_path))
    if https:
        _add_hsts(deployed)
        _site_update(deployed)
    assert site.read_text() == deployed.read_text()


@pytest.mark.parametrize("refused", [False, True])
def test_a_site_nginx_refuses_after_the_update_is_put_back(tmp_path, refused):
    # Left in place, the next reload (certbot's renewal, a reboot) would be refused for every site here.
    site = tmp_path / "lumiverb"
    site.write_text(OLD_SITE)
    text = UPDATE_WEB.read_text()
    steps = text[text.index('step "Updating the nginx site"'):text.index('ok "nginx reloaded"')]
    nginx = 'nginx() { [[ "$1" == -t ]] && return 1; return 0; }' if refused else "nginx() { :; }"
    script = (f'step() {{ :; }}; ok() {{ :; }}; warn() {{ :; }}; fail() {{ echo "$1"; exit 1; }}; {nginx}\n'
              f'APP_DIR={REPO}; LUMIVERB_NGINX_CONF_D={tmp_path}; export LUMIVERB_SITE_LOG_DIR={tmp_path}/log LUMIVERB_LOGROTATE_D={tmp_path}\n' + steps.replace("/etc/nginx/sites-available/lumiverb", str(site)))
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert (tmp_path / "lumiverb.before-update").read_text() == OLD_SITE
    if refused:
        assert out.returncode == 1 and "are back as they were before this update" in out.stdout, out.stdout + out.stderr
        assert site.read_text() == OLD_SITE
    else:
        assert out.returncode == 0, out.stdout + out.stderr
        assert site.read_text() == _deploy_web_site(tmp_path)


@pytest.mark.parametrize("refused", [False, True])
def test_update_says_nothing_of_nginx_s_version_on_deploy_site_s_sites_either(tmp_path, refused):
    # lumiverb.io's front page and the docs, beside the web app's site: certbot's redirects in them showed it.
    site = tmp_path / "lumiverb"
    site.write_text(_certbot(_site_v0_1_160()))
    static = tmp_path / "lumiverb-site"
    redirect = ("server {\n    if ($host = lumiverb.io) {\n        return 301 https://$host$request_uri;\n    }\n"
                "    listen 80;\n    server_name lumiverb.io;\n    return 404; # managed by Certbot\n}\n")
    static.write_text(redirect)
    (tmp_path / "lumiverb-default").write_text("server {\n    server_name _;\n}\n")  # the default site: its own
    text = UPDATE_WEB.read_text()
    steps = text[text.index('step "Updating the nginx site"'):text.index('ok "nginx reloaded"')]
    nginx = 'nginx() { [[ "$1" == -t ]] && return 1; return 0; }' if refused else "nginx() { :; }"
    script = (f'step() {{ :; }}; ok() {{ :; }}; warn() {{ :; }}; fail() {{ echo "$1"; exit 1; }}; {nginx}\n'
              f'install_default_site() {{ :; }}\nAPP_DIR={REPO}; LUMIVERB_NGINX_CONF_D={tmp_path}; export LUMIVERB_SITE_LOG_DIR={tmp_path}/log LUMIVERB_LOGROTATE_D={tmp_path}\n'
              + steps.replace("/etc/nginx/sites-available/lumiverb", str(site)))
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert (tmp_path / "lumiverb-default").read_text() == "server {\n    server_name _;\n}\n"
    if refused:
        assert out.returncode == 1, out.stdout + out.stderr
        assert static.read_text() == redirect and site.read_text() == _certbot(_site_v0_1_160()), "both put back"
    else:
        assert out.returncode == 0, out.stdout + out.stderr
        assert static.read_text() == redirect.replace(
            "    server_name lumiverb.io;\n",
            "    server_name lumiverb.io;\n    access_log /var/log/nginx/access.log lumiverb_host;\n"
            "    access_log /var/log/lumiverb/sites.log lumiverb_host;\n"
            "    server_tokens off;\n"), "its version unsaid, its logs naming the site"
        assert (tmp_path / "lumiverb-log.conf").exists(), "the format its log lines use, before a site uses it"
        assert site.read_text().count("server_tokens off;") == 2, "the app's block and certbot's redirect"


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


def test_tenant_migrations_log_to_a_private_temp_file():
    # A fixed /tmp path made by another user (a dev run as robert) can't be
    # written by the lumiverb user, so every tenant migration "failed" and
    # update-api.sh stopped before restarting anything.
    text = (REPO / "scripts" / "migrate.sh").read_text()
    assert "/tmp/lumiverb_alembic_tenant.log" not in text
    assert "mktemp" in text
    assert "trap" in text and "rm -f" in text


def test_update_restarts_the_scheduler_only_once_the_api_answers():
    """Else its first jobs meet a refused connection mid-restart."""
    text = UPDATE_API.read_text()
    step = text[text.index('step "Restarting services"'):]
    api, health, scheduler = (step.index("systemctl restart lumiverb-api"), step.index("/health"),
                              step.index("systemctl start lumiverb-scheduler"))
    assert api < health < scheduler



def test_an_update_without_a_data_dir_stops_before_changing_anything(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text("API_PORT=8100\n")
    text = UPDATE_API.read_text()
    block = text.split('step "Updating Python dependencies"', 1)[1].split("# ----", 1)[0]
    script = (
        "RED=''; NC=''\n"
        'step() { :; }; ok() { :; }; warn() { :; }; fail() { echo "fail: $1"; exit 1; }\n'
        'systemctl() { [[ "$1 $2" == "is-enabled lumiverb-scheduler" ]]; }\n'
        f'sudo() {{ echo "sudo $*"; }}\n'
        f'APP_DIR="{tmp_path}"; SVC_USER=lumiverb; UV_BIN=uv; ENV_FILE="{env_file}"\n'
        + _update_helpers() + block
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode != 0 and "fail: No DATA_DIR" in out.stdout
    assert "sync" not in out.stdout  # nothing installed first


def test_a_python_change_stops_the_old_worker_gently_too():
    # Review round 3: that path stopped lumiverb-worker before the drop-in existed.
    text = UPDATE_API.read_text()
    block = text.split('if [[ -n "$HAVE_PY" && -n "$WANT_PY" && "$HAVE_PY" != "$WANT_PY" ]]; then', 1)[1].split("\nfi\n", 1)[0]
    assert block.index("KillMode=mixed") < block.index("systemctl daemon-reload") < block.index(
        'systemctl stop "$unit"')


# --- Containers that use the GPU get it back after systemd reloads ----------
# systemctl daemon-reload takes the GPU from running containers
# (nvidia-container-toolkit with systemd's cgroups): prod's Ollama then
# ran its model on the CPU, with no error, until restarted (Oct 9).


def _gpu_step(tmp_path: Path, containers: dict[str, str], *, docker: bool = True) -> tuple[str, list[str]]:
    """Run update-api.sh's GPU step with docker stubbed. containers: name →
    "gpu" (has it), "lost" (lost it; a restart gives it back), "gone" (lost
    it for good), "no-smi" (uses the GPU, no nvidia-smi in it), "cpu" (no GPU asked for).
    Returns its output and the docker calls."""
    calls = tmp_path / "docker-calls"
    calls.write_text("")
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    for name, how in containers.items():
        (state / name).write_text(how)
    stub = (
        f'docker() {{ echo "$*" >> "{calls}"; S="{state}"\n'
        '  case "$1" in\n'
        '    ps) ls "$S" ;;\n'
        '    inspect) [[ "$(cat "$S/${@: -1}")" == cpu ]] || echo nvidia ;;\n'
        '    restart) [[ "$(cat "$S/$2")" == lost ]] && echo gpu > "$S/$2"; echo "$2" ;;\n'
        '    exec) case "$(cat "$S/$2")" in\n'
        '            gpu) echo "GPU 0: NVIDIA GeForce RTX 3080" ;;\n'
        '            lost|gone) echo "Failed to initialize NVML: Unknown Error"; return 255 ;;\n'
        '            no-smi) echo "exec: nvidia-smi: not found"; return 127 ;;\n'
        '          esac ;;\n'
        '  esac; }\n'
    )
    text = UPDATE_API.read_text()
    block = text.split('step "Giving the GPU back to containers"', 1)[1].split("# ----", 1)[0]
    script = (
        'step() { :; }; ok() { echo "ok: $1"; }; warn() { echo "warn: $1"; }\n'
        + (stub if docker else 'command() { [[ "$2" != docker ]] && builtin command "$@"; }\n')
        + block
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout + out.stderr, calls.read_text().splitlines()


def _restarted(calls: list[str]) -> list[str]:
    return [c.split()[1] for c in calls if c.startswith("restart ")]


def test_a_container_that_lost_the_gpu_is_restarted(tmp_path):
    out, calls = _gpu_step(tmp_path, {"ollama": "lost"})
    assert _restarted(calls) == ["ollama"]
    assert "ok: ollama" in out and "GPU again" in out


def test_one_that_has_it_is_left_running(tmp_path):
    out, calls = _gpu_step(tmp_path, {"ollama": "gpu"})
    assert _restarted(calls) == [] and "ok: ollama has the GPU" in out


def test_only_containers_that_use_the_gpu_are_looked_at(tmp_path):
    _, calls = _gpu_step(tmp_path, {"resolve_pgsql": "cpu", "ollama": "gpu"})
    assert not any(c.startswith("exec resolve_pgsql") for c in calls)


def test_one_that_cant_say_isnt_restarted(tmp_path):
    # No nvidia-smi in it: its GPU can't be checked, so it's left alone.
    out, calls = _gpu_step(tmp_path, {"trainer": "no-smi"})
    assert _restarted(calls) == [] and "warn" not in out


def test_one_a_restart_doesnt_fix_says_what_to_do(tmp_path):
    out, calls = _gpu_step(tmp_path, {"ollama": "gone"})
    assert _restarted(calls) == ["ollama"]
    assert "warn: ollama" in out and "docker restart ollama" in out


def test_without_docker_the_step_does_nothing(tmp_path):
    out, _ = _gpu_step(tmp_path, {}, docker=False)
    assert "warn" not in out


def test_the_gpu_comes_back_after_the_last_reload_and_before_the_scheduler_restarts():
    text = UPDATE_API.read_text()
    gpu = text.index('step "Giving the GPU back to containers"')
    assert text.rindex("systemctl daemon-reload") < gpu < text.rindex("systemctl start lumiverb-scheduler")


# --- The scheduler is stopped while the API restarts --------------------------
# Running on, its jobs couldn't save while the API was down, and each counted
# against its clip as a failure ("Connection refused" after the 08:49 update, Oct 9).


def _restart_calls(tmp_path: Path, *, answers: bool = True) -> list[str]:
    calls = tmp_path / "calls"
    calls.write_text("")
    text = UPDATE_API.read_text()
    block = text.split('step "Restarting services"', 1)[1].split('echo -e "${GREEN}${BOLD}API update complete', 1)[0]
    script = (
        'step() { :; }; ok() { :; }; warn() { :; }; fail() { echo "fail: $1"; exit 1; }\n'
        # A unit that's running isn't failed (is-failed's exit status).
        f'systemctl() {{ echo "systemctl $*" >> "{calls}"; [[ "$1" != is-failed ]]; }}\n'
        f'curl() {{ echo "curl $*" >> "{calls}"; {"" if answers else "return 7;"} }}\n'
        'sudo() { :; }; sleep() { :; }\n'
        'API_PORT=8100; SVC_USER=lumiverb; UV_BIN=uv\n'
        + block
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert (out.returncode == 0) == answers, out.stdout + out.stderr
    if not answers:
        assert "fail: API server not responding after 90 s" in out.stdout
    return [c for c in calls.read_text().splitlines()
            if not c.startswith(("systemctl is-enabled", "systemctl status", "systemctl is-failed"))]


def test_the_scheduler_stops_before_the_api_restarts_and_starts_once_it_answers(tmp_path):
    calls = _restart_calls(tmp_path)
    stop = calls.index("systemctl stop lumiverb-scheduler")
    api = calls.index("systemctl restart lumiverb-api")
    health = next(i for i, c in enumerate(calls) if c.startswith("curl") and "/health" in c)
    start = calls.index("systemctl start lumiverb-scheduler")
    assert stop < api < health < start


def test_an_api_that_doesnt_answer_in_90_s_fails_before_the_scheduler_starts(tmp_path):
    """Review: the scheduler was started whether or not the API answered."""
    calls = _restart_calls(tmp_path, answers=False)
    assert "systemctl start lumiverb-scheduler" not in calls
    assert sum(1 for c in calls if c.startswith("curl")) == 90


def test_a_failure_with_the_scheduler_stopped_says_how_to_start_it(tmp_path):
    _, _, err = _update_python(tmp_path, VENV_CFG.format("3.14.4"), fail_on="UV_NO_CONFIG=1 /usr/local/bin/uv sync", ok=False)
    assert "The scheduler is stopped" in err and "sudo systemctl start lumiverb-scheduler" in err


def test_under_update_sh_the_scheduler_line_is_update_sh_s(tmp_path, monkeypatch):
    """update.sh says it once, before its Result line."""
    monkeypatch.setenv("LUMIVERB_UPDATE_LOG", str(tmp_path / "log"))
    _, _, err = _update_python(tmp_path, VENV_CFG.format("3.14.4"), fail_on="UV_NO_CONFIG=1 /usr/local/bin/uv sync", ok=False)
    assert "isn't running" in err and "The scheduler is stopped" not in err


def test_a_running_scheduler_isnt_said_to_be_stopped(tmp_path):
    _, _, err = _update_python(tmp_path, VENV_CFG.format("3.14.4"), fail_on="python install", ok=False)
    assert "The scheduler is stopped" not in err


def test_deploy_stops_the_scheduler_before_the_migrations():
    """Review: deploy-api.sh migrated with the old scheduler running; its
    "Starting the scheduler" step starts it again."""
    text = DEPLOY_API.read_text()
    step = text.index('step "Running database migrations"')
    stop = text.index("systemctl stop lumiverb-scheduler", step)
    assert step < stop < text.index("alembic -c alembic-control.ini upgrade head")
    assert stop < text.index("systemctl restart lumiverb-scheduler")


DEPLOY_VPS = REPO / "scripts" / "deploy-vps.sh"


def _vps_dry_run(tmp_path: Path, *args: str, env_file: str | None = None, script: Path = DEPLOY_VPS,
                 extra_env: dict[str, str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """deploy-vps.sh --dry-run: the API half's settings, then the web half's."""
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    if env_file is not None:
        (conf / "env").write_text(env_file)
    out = subprocess.run(
        ["bash", str(script), "--domain", "app.lumiverb.test", *args, "--dry-run"],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "LUMIVERB_CONF_DIR": str(conf), **(extra_env or {})},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    web = lines.index("DOMAIN=app.lumiverb.test")
    return (dict(line.split("=", 1) for line in lines[:web]),
            dict(line.split("=", 1) for line in lines[web:]))


def test_a_vps_dry_run_dry_runs_both_halves_and_changes_nothing(tmp_path):
    # The web half used to run for real: nginx, Node.js, a build, a certificate.
    data = tmp_path / "data"
    api, web = _vps_dry_run(tmp_path, "--data-dir", str(data))
    assert api["DATA_DIR"] == str(data)
    assert web["DOMAIN"] == "app.lumiverb.test"
    assert not data.exists()


@pytest.mark.parametrize(("args", "env_file", "upstream"), [
    ((), None, "http://127.0.0.1:8000"),
    ((), REMEMBERED, "http://127.0.0.1:8100"),
    (("--api-port", "8200"), REMEMBERED, "http://127.0.0.1:8200"),
    (("--api-listen-host", "10.0.0.5"), REMEMBERED, "http://10.0.0.5:8100"),
    (("--api-listen-host", "0.0.0.0"), None, "http://127.0.0.1:8000"),
    (("--api-listen-host", "::1"), None, "http://[::1]:8000"),
], ids=["defaults", "remembered", "flag-beats-remembered", "one-address", "every-address", "ipv6"])
def test_the_web_half_proxies_to_where_the_api_half_listens(tmp_path, args, env_file, upstream):
    # It was always 127.0.0.1:8000, whatever --api-port or the env file said.
    api, web = _vps_dry_run(tmp_path, *args, env_file=env_file)
    assert web["API_UPSTREAM"] == upstream
    assert upstream.endswith(f":{api['API_PORT']}")


@pytest.mark.parametrize(("flag", "no_firewall"), [("--firewall", "false"), ("--no-firewall", "true")])
def test_firewall_flags_reach_both_halves(tmp_path, flag, no_firewall):
    remembered = REMEMBERED if flag == "--firewall" else REMEMBERED.replace("NO_FIREWALL=true", "NO_FIREWALL=false")
    api, web = _vps_dry_run(tmp_path, flag, env_file=remembered)
    assert (api["NO_FIREWALL"], web["NO_FIREWALL"]) == (no_firewall, no_firewall)


def _fake_bin(tmp_path: Path, name: str, body: str) -> None:
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def test_a_curl_pipe_dry_run_reads_the_scripts_from_a_throwaway_clone(tmp_path):
    # Piped from GitHub there are no sibling scripts: a real run clones into
    # /opt/lumiverb (installing git with apt). A dry run touches neither.
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("deploy-api.sh", "deploy-web.sh"):
        (repo / "scripts" / name).write_text((REPO / "scripts" / name).read_text())
    git_env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "scripts"]):
        subprocess.run(["git", *cmd], cwd=repo, env=git_env, check=True, capture_output=True)
    piped = tmp_path / "piped" / "deploy-vps.sh"
    piped.parent.mkdir()
    piped.write_text(DEPLOY_VPS.read_text())
    _fake_bin(tmp_path, "apt-get", f'echo "$@" >> {tmp_path}/apt-calls\n')
    temp = tmp_path / "temp"
    temp.mkdir()
    api, web = _vps_dry_run(tmp_path, "--repo", f"file://{repo}", "--branch", "main", env_file=REMEMBERED,
                            script=piped, extra_env={"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
                                                     "TMPDIR": str(temp), **{k: v for k, v in git_env.items()
                                                                              if k.startswith("GIT_CONFIG")}})
    assert web["API_UPSTREAM"] == "http://127.0.0.1:8100"
    assert not (tmp_path / "apt-calls").exists()
    assert list(temp.iterdir()) == [], "the clone is gone"


def test_a_real_run_proxies_to_the_port_the_api_half_wrote_and_says_what_next(tmp_path):
    # Both halves faked: the API half writes the env file as deploy-api.sh does.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "deploy-vps.sh").write_text(DEPLOY_VPS.read_text())
    conf = tmp_path / "conf"
    conf.mkdir()
    (scripts / "deploy-api.sh").write_text(f'printf "API_LISTEN_HOST=127.0.0.1\\nAPI_PORT=8100\\n" > {conf}/env\n')
    (scripts / "deploy-web.sh").write_text(f'echo "$@" > {tmp_path}/web-args\n')
    _fake_bin(tmp_path, "id", '[ "$1" = "-u" ] && echo 0 || /usr/bin/id "$@"\n')
    out = subprocess.run(
        ["bash", str(scripts / "deploy-vps.sh"), "--domain", "app.lumiverb.test", "--api-port", "8100",
         "--no-firewall"],
        capture_output=True, text=True, timeout=30, check=False,
        env={**os.environ, "LUMIVERB_CONF_DIR": str(conf), "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert (tmp_path / "web-args").read_text().split() == [
        "--domain", "app.lumiverb.test", "--api-upstream", "http://127.0.0.1:8100", "--no-firewall"]
    # The README's next steps, not the retired update-vps.sh.
    assert f"sudo /opt/lumiverb/.venv/bin/lumiverb-server setup --env {conf}/env" in out.stdout
    assert "sudo bash /opt/lumiverb/scripts/update.sh" in out.stdout
    assert "update-vps.sh" not in out.stdout


def test_neither_an_install_nor_an_update_lets_lumiverb_read_the_whole_journal():
    # systemd-journal reads every service's lines and sudo's command lines, not
    # only Lumiverb's: whoever runs the server grants it, told so by Admin ›
    # Logs (src/server/service_log.py), never these scripts (Robert, Oct 11).
    for script in (DEPLOY_API, UPDATE_API, REPO / "scripts" / "update.sh", DEPLOY_VPS):
        assert "systemd-journal" not in script.read_text(), script.name


# --- Quickwit leaves the install (ADR-019) -----------------------------------
# Search is PostgreSQL's own, in each account's database: a new install gets
# no Quickwit, and an update takes an earlier install's away, nothing else.


def test_a_new_install_has_no_quickwit(tmp_path):
    text = DEPLOY_API.read_text()
    # Nothing downloaded, configured, made, remembered or started for it.
    for gone in ("QUICKWIT_VERSION", "quickwit-oss", "quickwit.yaml", "lumiverb-quickwit.service <<",
                 '"$DATA_DIR"/quickwit', "QW_PORT", "QW_GRPC_PORT", "_existing_val QUICKWIT", "--quickwit-port"):
        assert gone not in text, gone
    env_block = text.split('cat > "$ENV_FILE" <<ENVEOF', 1)[1].split("ENVEOF", 1)[0]
    assert "QUICKWIT" not in env_block
    api_unit = text.split("Description=Lumiverb API Server", 1)[1].split("UNIT", 1)[0]
    assert "quickwit" not in api_unit
    assert [line for line in text.splitlines()
            if line.startswith(("systemctl enable", "systemctl status")) and "quickwit" in line] == []
    assert "QUICKWIT_PORT" not in _settings(tmp_path, "--app-host", "http://x")
    # An old command line's --quickwit-port stops the run before it does anything.
    out = subprocess.run(["bash", str(DEPLOY_API), "--app-host", "http://x", "--quickwit-port", "7290", "--dry-run"],
                         capture_output=True, text=True, timeout=30, check=False,
                         env={**os.environ, "LUMIVERB_CONF_DIR": str(tmp_path / "conf")})
    assert out.returncode != 0 and "Unknown option: --quickwit-port" in out.stderr


@pytest.mark.parametrize("had_it", [True, False], ids=["had-quickwit", "never-had-it"])
def test_a_rerun_over_an_earlier_quickwit_says_the_update_removes_it(tmp_path, had_it):
    text = DEPLOY_API.read_text()
    start = text.index("if [[ -f /etc/systemd/system/lumiverb-quickwit.service ]]")
    block = text[start:text.index("\nfi\n", start) + 4]
    units = tmp_path / "units"
    units.mkdir()
    if had_it:
        (units / "lumiverb-quickwit.service").write_text("[Service]\n")
    script = 'warn() { echo "warn: $1"; }\nAPP_DIR=/opt/lumiverb\n' + block.replace("/etc/systemd/system/", f"{units}/")
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    said = ("warn: Quickwit from an earlier install is still here, unused: "
            "sudo bash /opt/lumiverb/scripts/update.sh removes it\n")
    assert out.stdout == (said if had_it else "")


# The API's unit as deploy-api.sh wrote it while it installed Quickwit.
OLD_API_UNIT = """\
[Unit]
Description=Lumiverb API Server
After=network.target postgresql.service lumiverb-quickwit.service
Wants=lumiverb-quickwit.service

[Service]
Type=simple
User=lumiverb
Group=lumiverb
WorkingDirectory=/opt/lumiverb
EnvironmentFile=/etc/lumiverb/env
Environment=PYTHONUNBUFFERED=1
Environment=TMPDIR={data}/tmp
ExecStartPre=-/usr/bin/find {data}/tmp -mindepth 1 -delete
ExecStart=/opt/lumiverb/.venv/bin/uvicorn src.server.api.main:app --host ${{API_LISTEN_HOST}} --port ${{API_PORT}} --workers 2
ReadWritePaths={data}

[Install]
WantedBy=multi-user.target
"""

# Its env file then: Quickwit's settings among the rest.
OLD_ENV = ("API_PORT=8100\nQUICKWIT_URL=http://127.0.0.1:7290\nQUICKWIT_ENABLED=true\nQUICKWIT_PORT=7290\n"
           "SMTP_HOST=smtp.fastmail.com\n")


def _install_with_quickwit(tmp_path: Path) -> dict[str, Path]:
    """What deploy-api.sh left while it installed Quickwit, under tmp_path, with
    things beside each of Quickwit's that must stay."""
    root = tmp_path / "root"
    p = {name: root / name for name in ("units", "conf", "data", "bin")}
    files = {
        "units/lumiverb-quickwit.service": "[Service]\nExecStart=/usr/local/bin/quickwit run --config "
                                           "/etc/lumiverb/quickwit.yaml\n",
        "units/lumiverb-quickwit.service.d/override.conf": "[Service]\nRestartSec=10s\n",
        "units/lumiverb-scheduler.service": "[Service]\n",
        "conf/quickwit.yaml": "version: 0.8\nnode_id: lumiverb\n",
        "conf/env": OLD_ENV,
        "conf/.pg_password": "pw",
        "data/quickwit/indexes/lumiverb_assets/01H.split": "index",
        "data/quickwit-before-0.8/keep": "someone's",
        "data/previews/a/b.jpg": "jpeg",
        "data/backups/20261015/control.dump": "dump",
        "bin/quickwit": "#!/bin/sh\n",
        "bin/uv": "#!/bin/sh\n",
        # Another Quickwit, on the PATH but not where deploy-api.sh put its own.
        "opt/quickwit/bin/quickwit": "#!/bin/sh\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (p["units"] / "lumiverb-api.service").write_text(OLD_API_UNIT.format(data=p["data"]))
    return p


def _update_quickwit(tmp_path: Path, p: dict[str, Path], *, data_dir: str | None = None) -> tuple[str, list[str]]:
    """Run update-api.sh's Quickwit step, then its API-unit step, on `p`;
    returns what they said and the systemctl calls."""
    calls = tmp_path / "systemctl-calls"
    calls.write_text("")
    text = UPDATE_API.read_text()
    start = text.index('QW_UNIT="')
    quickwit = text[start:text.index("# ----", start)]
    api_unit = text.split('step "Updating the API unit"', 1)[1].split("# ----", 1)[0]
    script = (
        'step() { echo "step: $1"; }; ok() { echo "ok: $1"; }; warn() { echo "warn: $1"; }\n'
        f'systemctl() {{ echo "$*" >> "{calls}"; }}\n'
        f'CONF_DIR="{p["conf"]}"; ENV_FILE="{p["conf"]}/env"; APP_DIR=/opt/lumiverb\n'
        f'DATA_DIR="{p["data"] if data_dir is None else data_dir}"\n'
        + (quickwit + api_unit).replace("/etc/systemd/system/", f"{p['units']}/")
                               .replace("/usr/local/bin/quickwit", f"{p['bin']}/quickwit")
    )
    path = f"{p['units'].parent / 'opt' / 'quickwit' / 'bin'}:{os.environ['PATH']}"
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30,
                         env={**os.environ, "PATH": path})
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout + out.stderr, calls.read_text().splitlines()


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _quickwit_s(name: str) -> bool:
    return (name in ("units/lumiverb-quickwit.service", "conf/quickwit.yaml", "data/quickwit", "bin/quickwit")
            or name.startswith(("units/lumiverb-quickwit.service.d", "data/quickwit/")))


def _deploy_api_unit_section() -> str:
    """The [Unit] section deploy-api.sh writes for the API now."""
    text = DEPLOY_API.read_text()
    return text.split("cat > /etc/systemd/system/lumiverb-api.service <<UNIT\n", 1)[1].split("\n\n", 1)[0]


def test_an_update_takes_quickwit_away_and_nothing_else(tmp_path):
    p = _install_with_quickwit(tmp_path)
    root = p["units"].parent
    before = _tree(root)
    out, calls = _update_quickwit(tmp_path, p)
    # Its unit, drop-ins, config, index and binary go; nothing beside them does,
    # in the data dir (a folder named like it included), the config folder or
    # /usr/local/bin, and no other Quickwit on the PATH.
    assert _tree(root) == [name for name in before if not _quickwit_s(name)]
    # Stopped and disabled before its unit goes, then systemd told.
    assert calls[:2] == ["disable --now lumiverb-quickwit", "daemon-reload"]
    # The env file is the admin's: its Quickwit settings stay, and the update says once that nothing reads them.
    assert (p["conf"] / "env").read_text() == OLD_ENV
    assert [line for line in out.splitlines() if "QUICKWIT_" in line] == [
        f"ok: QUICKWIT_URL, QUICKWIT_ENABLED, QUICKWIT_PORT in {p['conf']}/env: no longer used, they may be removed"]
    assert "step: Removing Quickwit (search is in PostgreSQL now)" in out
    assert "warn" not in out
    # The API's unit neither waits for Quickwit nor wants it: its [Unit] is a
    # new install's, the rest as it was but uvicorn's start (no process of it dumpable).
    unit, rest = (p["units"] / "lumiverb-api.service").read_text().split("\n\n", 1)
    assert unit == _deploy_api_unit_section()
    assert rest == _not_dumpable(OLD_API_UNIT.format(data=p["data"]).split("\n\n", 1)[1])
    assert "ok: The API no longer waits for Quickwit" in out


def _not_dumpable(unit: str) -> str:
    """An old API unit's text as an update leaves it: uvicorn started through python -m src.server.api."""
    old = "ExecStart=/opt/lumiverb/.venv/bin/uvicorn src.server.api.main:app"
    assert unit.count(old) == 1
    return unit.replace(old, "ExecStart=/opt/lumiverb/.venv/bin/python -m src.server.api src.server.api.main:app")


def test_a_second_update_has_nothing_to_do_and_says_nothing(tmp_path):
    p = _install_with_quickwit(tmp_path)
    _update_quickwit(tmp_path, p)
    root = p["units"].parent
    before, unit = _tree(root), (p["units"] / "lumiverb-api.service").read_text()
    out, calls = _update_quickwit(tmp_path, p)
    assert out == ""
    assert not [call for call in calls if "quickwit" in call]
    assert _tree(root) == before
    assert (p["units"] / "lumiverb-api.service").read_text() == unit


def test_an_install_that_never_had_quickwit_is_left_alone(tmp_path):
    # A new install's: no Quickwit, an env file that never named it, the API unit deploy-api.sh writes now.
    p = _install_with_quickwit(tmp_path)
    root = p["units"].parent
    for name in ("units/lumiverb-quickwit.service", "conf/quickwit.yaml", "bin/quickwit"):
        (root / name).unlink()
    for name in ("units/lumiverb-quickwit.service.d", "data/quickwit"):
        shutil.rmtree(root / name)
    (p["conf"] / "env").write_text("API_PORT=8100\n")
    api_unit = p["units"] / "lumiverb-api.service"
    old = OLD_API_UNIT.format(data=p["data"])
    api_unit.write_text(_deploy_api_unit_section() + "\n\n" + _not_dumpable(old.split("\n\n", 1)[1]))
    before, unit = _tree(root), api_unit.read_text()
    out, calls = _update_quickwit(tmp_path, p)
    assert out == ""
    assert not [call for call in calls if "quickwit" in call]
    assert _tree(root) == before
    assert api_unit.read_text() == unit


def test_quickwit_s_folder_as_a_link_goes_but_never_what_it_points_to(tmp_path):
    # Moved to another disk by hand: the link in the data dir goes, nothing on that disk.
    p = _install_with_quickwit(tmp_path)
    shutil.rmtree(p["data"] / "quickwit")
    elsewhere = tmp_path / "other-disk" / "quickwit"
    elsewhere.mkdir(parents=True)
    (elsewhere / "01H.split").write_text("index")
    (p["data"] / "quickwit").symlink_to(elsewhere)
    out, _ = _update_quickwit(tmp_path, p)
    assert not (p["data"] / "quickwit").is_symlink() and not (p["data"] / "quickwit").exists()
    assert (elsewhere / "01H.split").read_text() == "index"
    assert f"ok: Removed {p['data']}/quickwit (its index)" in out


def test_without_a_data_dir_no_folder_is_taken(tmp_path):
    # "${DATA_DIR}/quickwit" with no DATA_DIR would be /quickwit.
    p = _install_with_quickwit(tmp_path)
    out, _ = _update_quickwit(tmp_path, p, data_dir="")
    assert "(its index)" not in out
    assert (p["data"] / "quickwit" / "indexes" / "lumiverb_assets" / "01H.split").exists()
    assert not (p["units"] / "lumiverb-quickwit.service").exists()  # the rest still goes


@pytest.mark.parametrize(("before", "after"), [
    (["After=network.target postgresql.service lumiverb-quickwit.service", "Wants=lumiverb-quickwit.service"],
     ["After=network.target postgresql.service"]),
    (["After=lumiverb-quickwit.service network.target", "Wants=lumiverb-quickwit.service  postgresql.service"],
     ["After=network.target", "Wants=postgresql.service"]),
    # Required, a unit that's gone would keep the API from starting.
    (["After=network.target lumiverb-quickwit.service postgresql.service", "Requires=lumiverb-quickwit.service"],
     ["After=network.target postgresql.service"]),
    (["After=network.target postgresql.service"], ["After=network.target postgresql.service"]),
], ids=["as-deployed", "edited-by-hand", "required", "already-without"])
def test_the_api_unit_stops_waiting_for_quickwit(tmp_path, before, after):
    unit = tmp_path / "lumiverb-api.service"
    unit.write_text("\n".join(["[Unit]", "Description=Lumiverb API Server", *before]) + "\n\n" + API_UNIT)
    _update_api_unit(tmp_path, unit)
    _update_api_unit(tmp_path, unit)  # reruns change nothing
    head, rest = unit.read_text().split("\n\n", 1)
    assert head.splitlines() == ["[Unit]", "Description=Lumiverb API Server", *after]
    assert rest.startswith("[Service]\n")


def test_an_update_no_longer_makes_restarts_or_shows_quickwit(tmp_path):
    text = UPDATE_API.read_text()
    assert "Fixing Quickwit sandbox" not in text
    assert "quickwit" not in text.split('step "Restarting services"', 1)[1]
    # Taken away before the API's unit stops naming it, and before anything restarts.
    assert text.index('QW_UNIT="') < text.index('step "Updating the API unit"') < text.index(
        'step "Restarting services"')
    # The data-dir step no longer makes its folder.
    data = tmp_path / "data"
    _update_data_dir(tmp_path, f"DATA_DIR={data}\n")
    assert sorted(path.name for path in data.iterdir()) == ["tmp", "worker-tmp"]


def test_a_fresh_web_install_opens_port_80_before_asking_for_its_certificate():
    # Let's Encrypt checks the domain on port 80: deploy-api.sh turns ufw on with SSH alone, so a certificate asked
    # for before deploy-web.sh opened 80 always failed on a fresh machine (the demo server's first install).
    text = (REPO / "scripts" / "deploy-web.sh").read_text()
    assert text.index("ufw allow 80/tcp") < text.index('certbot "${certbot_args[@]}"')
    assert text.index("ufw allow 80/tcp") < text.index("certbot install --nginx")


def _upkeep_calls(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("ExecStart=") and "/v1/upkeep" in line]


def test_upkeep_waits_for_an_api_still_starting_and_an_update_writes_the_same():
    # A timer enabled on a machine up longer than OnBootSec fires at once, before the API listens: its curl failed
    # (connection refused) and left the unit failed after a clean install.
    deploy, update = _upkeep_calls(DEPLOY_API.read_text()), _upkeep_calls(UPDATE_API.read_text())
    assert len(deploy) == 2 and deploy == update
    assert all("exec /usr/bin/curl -sf --retry 12 --retry-delay 5 --retry-connrefused -X POST " in call
               for call in deploy)


def _as_systemd_runs_it(line: str) -> str:
    """The shell script an upkeep unit's ExecStart=/bin/sh -c '…' hands sh, as written by the scripts' heredocs
    (bash's \\$ is $) and then read by systemd ($$ is a $, %% a %, \\n a line break in a quoted word)."""
    line = line.replace("\\$", "$")
    script = line.split("ExecStart=/bin/sh -c '", 1)[1].rsplit("'", 1)[0]
    return script.replace("$$", "$").replace("%%", "%").replace("\\n", "\n")


@pytest.mark.parametrize("which", [0, 1], ids=["every-5-minutes", "daily"])
def test_the_install_key_never_reaches_curl_as_an_argument(tmp_path, which):
    # A command line is every local user's to read (ps). The key reaches curl on its stdin; nothing else changes.
    line = _upkeep_calls(DEPLOY_API.read_text())[which]
    assert "ADMIN_KEY}" not in line.split("exec /usr/bin/curl", 1)[1], "never one of curl's arguments"
    script = _as_systemd_runs_it(line)
    seen = tmp_path / "seen"
    _fake_bin(tmp_path, "curl", f'printf "%s\\n" "$@" > {seen}.args\ncat > {seen}.stdin\n')
    out = subprocess.run(["/bin/sh", "-c", script.replace("/usr/bin/curl", str(tmp_path / "bin" / "curl"))],
                         capture_output=True, text=True, timeout=30,
                         env={"PATH": "/usr/bin:/bin", "ADMIN_KEY": "k-7f3a-install", "API_PORT": "8100"})
    assert out.returncode == 0, out.stderr
    args = (tmp_path / "seen.args").read_text().splitlines()
    assert "k-7f3a-install" not in " ".join(args)
    assert args == ["-sf", "--retry", "12", "--retry-delay", "5", "--retry-connrefused", "-X", "POST",
                    "http://127.0.0.1:8100/v1/upkeep" + ("?tenants=all" if which == 0 else
                                                         "/cleanup?tenants=all&dry_run=false"),
                    "-H", "@-", "-H", "Content-Type: application/json"]
    assert (tmp_path / "seen.stdin").read_text() == "Authorization: Bearer k-7f3a-install\n"


def test_the_first_accounts_provisioning_keeps_the_key_off_curls_command_line():
    text = DEPLOY_API.read_text()
    provision = text.split('step "Provisioning tenant: ${TENANT_NAME}"', 1)[1].split('ok "CLI configured', 1)[0]
    assert "-H @<(printf 'Authorization: Bearer %s\\n' \"$ADMIN_KEY\")" in provision
    assert "--data-binary @-" in provision and '-d "${TENANT_BODY}"' not in provision
    assert "Bearer ${ADMIN_KEY}" not in text



def _install_checks(tmp_path: Path, *, fstab: str = "", mounted: tuple[str, ...] = (), disks: str = "[]",
                    meminfo: str = "MemTotal: 16000000 kB\nSwapTotal: 0 kB\n", other_disk: tuple[str, ...] = (),
                    calls: str = "") -> tuple[int, str]:
    """deploy-api.sh's checks before anything's made, with the machine stubbed: fstab's lines, the mount points
    mounted, lsblk's disks (its JSON "blockdevices"), /proc/meminfo, folders on another disk than /."""
    (tmp_path / "fstab").write_text(fstab)
    (tmp_path / "meminfo").write_text(meminfo)
    text = DEPLOY_API.read_text()
    block = text.split("# Before anything's made: where the data goes, and memory\n", 1)[1].split(
        '\n# ---------------------------------------------------------------------------\n\ndata_dir_check', 1)[0]
    script = (
        'fail() { echo "fail: $1"; exit 1; }; warn() { echo "warn: $1"; }\n'
        + block
        + f'\nFSTAB="{tmp_path}/fstab"; MEMINFO="{tmp_path}/meminfo"\n'
        + f'mountpoint() {{ case "$2" in {"|".join(mounted) or "''"}) return 0 ;; *) return 1 ;; esac; }}\n'
        + f"lsblk() {{ echo '{{\"blockdevices\": {disks}}}'; }}\n"
        + f'stat() {{ case "${{@: -1}}" in {"|".join(other_disk) or "''"}) echo 2 ;; *) echo 1 ;; esac; }}\n'
        + calls
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    return out.returncode, out.stdout + out.stderr


SDA = '[{"name": "/dev/vda", "type": "disk", "size": "25G", "children": [{"name": "/dev/vda1", "type": "part", ' \
      '"size": "25G", "mountpoint": "/"}]}, {"name": "/dev/sda", "type": "disk", "size": "100G", "mountpoint": null}]'


def test_a_data_dir_whose_drive_fstab_lists_isnt_mounted_is_refused(tmp_path):
    # The demo server's install: a Volume meant for the data, not mounted, would have put everything on the root disk.
    fstab = "# comment /mnt/demo\nUUID=1 / ext4 defaults 0 1\n/dev/sda /mnt/demo ext4 defaults,nofail 0 2\n"
    rc, out = _install_checks(tmp_path, fstab=fstab, calls='data_dir_check /mnt/demo/lumiverb false\n')
    assert rc == 1 and "fail: There's no drive at /mnt/demo (/etc/fstab mounts one there)" in out, out
    assert _install_checks(tmp_path, fstab=fstab, mounted=("/mnt/demo",),
                           calls='data_dir_check /mnt/demo/lumiverb/ false\n') == (0, "")
    # The deepest mount point holding it; a name that only starts the same isn't under it.
    nested = fstab + "/dev/sdb /mnt ext4 defaults 0 2\n"
    rc, out = _install_checks(tmp_path, fstab=nested, mounted=("/mnt",), calls='data_dir_check /mnt/demo/x false\n')
    assert rc == 1 and "at /mnt/demo " in out, out
    assert _install_checks(tmp_path, fstab=fstab, calls='data_dir_check /mnt/demolition/x false\n') == (0, "")


def test_a_new_data_dir_on_the_system_disk_beside_an_unmounted_disk_is_refused_until_made(tmp_path):
    data = tmp_path / "lumiverb"
    rc, out = _install_checks(tmp_path, disks=SDA, calls=f'data_dir_check {data} true\n')
    assert rc == 1 and f"fail: {data} would be a new folder on the system disk" in out and "/dev/sda (100G)" in out
    assert f"sudo mkdir -p {data}" in out, "how to keep it there"
    data.mkdir()
    assert _install_checks(tmp_path, disks=SDA, calls=f'data_dir_check {data} true\n') == (0, ""), "made first: meant"
    other = tmp_path / "elsewhere"
    assert _install_checks(tmp_path, disks=SDA, calls=f'data_dir_check {other} false\n') == (0, ""), "the default"
    assert _install_checks(tmp_path, disks=SDA, other_disk=(str(tmp_path),),
                           calls=f'data_dir_check {other} true\n') == (0, ""), "on another disk already"
    used = SDA.replace('"mountpoint": null', '"mountpoint": "[SWAP]"')
    assert _install_checks(tmp_path, disks=used, calls=f'data_dir_check {other} true\n') == (0, ""), "swap is use"


def test_little_memory_and_no_swap_is_said(tmp_path):
    small = "MemTotal:        3995000 kB\nMemFree: 1 kB\nSwapTotal:             0 kB\n"
    rc, out = _install_checks(tmp_path, meminfo=small, calls='memory_check\necho "$LOW_MEMORY"\n')
    assert rc == 0 and out.startswith("warn: This machine has 3.8 GB of memory and no swap."), out
    assert "add swap or memory" in out and "DigitalOcean" not in out
    swapped = small.replace("SwapTotal:             0", "SwapTotal: 4194300")
    assert _install_checks(tmp_path, meminfo=swapped, calls='memory_check\n') == (0, "")
    assert _install_checks(tmp_path, calls='memory_check\n') == (0, ""), "16 GB"
    text = DEPLOY_API.read_text()
    assert text.index("memory_check\n") < text.index('mkdir -p "$DATA_DIR"'), "said before anything's made"
    assert text.count('warn "$LOW_MEMORY"') == 2, "and again at the end"


def _points_here(tmp_path: Path, resolved: list[str], mine: list[str]) -> tuple[int, str]:
    """deploy-web.sh's domain check, DNS (getent) and this machine's addresses (ip) stubbed."""
    text = DEPLOY_WEB.read_text()
    block = text.split("# Whether the domain points here", 1)[1].split("# Said first too", 1)[0]
    getent = "".join(f"echo '{a}       STREAM try.example.org'\n" for a in resolved) or ":"
    ip = "".join(f"echo '2: eth0    inet {a}/20 brd x scope global eth0'\n" for a in mine) or ":"
    script = (
        'warn() { echo "warn: $1"; }\n' + block
        + f"getent() {{ {getent}\n}}\nip() {{ {ip}\n}}\n"
        + 'domain_points_here try.example.org\n'
    )
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    return out.returncode, out.stdout + out.stderr


def test_the_domain_is_checked_before_lets_encrypt_is_asked(tmp_path):
    public, home = "167.99.234.121", "192.168.86.166"
    rc, out = _points_here(tmp_path, [], [public, "10.10.0.5"])
    assert rc == 1 and out.strip() == (
        "warn: try.example.org doesn't resolve yet: add an A record for it pointing at 167.99.234.121"), out
    assert _points_here(tmp_path, [public], [public]) == (0, "")
    rc, out = _points_here(tmp_path, ["203.0.113.9"], [public])
    assert rc == 1 and "Its A record points at 203.0.113.9: change it to 167.99.234.121." in out, out
    assert _points_here(tmp_path, ["203.0.113.9"], [home]) == (0, ""), "behind a router: can't tell"
    rc, out = _points_here(tmp_path, [public, "2001:db8::9"], [public, "2604:a880::1"])
    assert rc == 1 and "Its AAAA record points at 2001:db8::9: change it to 2604:a880::1, or remove it." in out
    assert _points_here(tmp_path, [public, "2001:db8::9"], [public]) == (0, ""), "no IPv6 here"
    assert _points_here(tmp_path, ["127.0.1.1"], [public]) == (0, ""), "/etc/hosts names this machine"
    text = DEPLOY_WEB.read_text()
    assert text.index('if ! domain_points_here "$DOMAIN"; then\n    warn "Not asked for yet') < text.index(
        'certbot "${certbot_args[@]}"')


def test_a_demo_is_exposed_only_as_a_demo_from_its_install(tmp_path):
    # From deploy until the runbook's step 8, every route answered from the internet, signing in included.
    assert _settings(tmp_path, "--app-host", "http://x")["EXPOSURE"] == ""
    assert _settings(tmp_path, "--app-host", "http://x", "--exposure", "demo")["EXPOSURE"] == "demo"
    assert _settings(tmp_path, env_file=REMEMBERED + "LUMIVERB_EXPOSURE=demo\n")["EXPOSURE"] == "demo", "remembered"
    refused = subprocess.run(["bash", str(DEPLOY_API), "--app-host", "http://x", "--exposure", "public", "--dry-run"],
                             capture_output=True, text=True, timeout=30,
                             env={**os.environ, "LUMIVERB_CONF_DIR": str(tmp_path)})
    assert refused.returncode == 1 and "--exposure (LUMIVERB_EXPOSURE) takes demo: public" in refused.stderr

    env = tmp_path / "env"
    _write_env(env, "EXPOSURE=demo")
    written = env.read_text()
    assert "\nLUMIVERB_EXPOSURE=demo\n" in written and "Added by hand" not in written
    _write_env(env, "EXPOSURE=demo")
    assert env.read_text().count("LUMIVERB_EXPOSURE=") == 1, "a rerun writes it once, as its own"
    _write_env(other := tmp_path / "other")
    assert "LUMIVERB_EXPOSURE" not in other.read_text()
    assert "only what LUMIVERB_EXPOSURE=${EXPOSURE} lists answers" in DEPLOY_API.read_text(), "said in what's next"


# --- What the service user can write never steers what root does ------------
# The service user owns the checkout, DATA_DIR and its home (on a new install, its home is DATA_DIR, which the API
# writes). Root's scripts run git, uv and npm as it, never with its own settings for them; root runs no git in the
# checkout; root's downloads go in a folder of its own.

SCRIPTS = {name: REPO / "scripts" / name for name in
           ("deploy-api.sh", "update-api.sh", "update.sh", "update-web.sh", "deploy-web.sh", "deploy-vps.sh")}


def test_git_npm_and_uv_run_as_the_service_user_without_its_own_settings():
    for name, path in SCRIPTS.items():
        for line in path.read_text().splitlines():
            run = re.sub(r'\b(fail|warn|ok|echo|step) "[^"]*"', "", line.strip())  # what it says, not runs
            if run.startswith("#") or not re.search(
                    r"\bgit (fetch|pull|checkout|log|rev-parse|rev-list|symbolic-ref|cat-file|check-ref-format|show"
                    r"|clone|config)\b|\bnpm (ci|run|install)\b|\"\$UV_BIN\" (sync|python|cache)|\$UV run", run):
                continue
            if run.startswith(("git config --system", "git config --global", "git clone ", "command -v git")):
                continue  # root's own config, or a new clone (nobody else's yet)
            assert ("as_svc " in run or '"${SVC_ENV[@]}"' in run
                    or "env GIT_CONFIG_GLOBAL=/dev/null git " in run), f"{name}: {run}"
    for name in ("deploy-api.sh", "update-api.sh"):
        assert ('SVC_ENV=(env GIT_CONFIG_GLOBAL=/dev/null npm_config_userconfig=/dev/null UV_NO_CONFIG=1)\n'
                'as_svc() { sudo -u "$SVC_USER" "${SVC_ENV[@]}" "$@"; }') in SCRIPTS[name].read_text(), name
    for name in ("update-web.sh", "deploy-web.sh"):
        assert ('as_svc() { sudo -u "$SVC_USER" env GIT_CONFIG_GLOBAL=/dev/null npm_config_userconfig=/dev/null '
                '"$@"; }') in SCRIPTS[name].read_text(), name
    assert 'as_svc() { sudo -u "$SVC_USER" env GIT_CONFIG_GLOBAL=/dev/null "$@" </dev/null; }' in SCRIPTS[
        "update.sh"].read_text()


def test_root_never_marks_the_service_users_checkout_safe_and_unmarks_it():
    # safe.directory let the checkout's own .git/config (an fsmonitor, hooks) steer a git root ran there.
    for name, path in SCRIPTS.items():
        text = path.read_text()
        assert "--add safe.directory" not in text and "--replace-all safe.directory" not in text, name
    for name in ("deploy-api.sh", "update-api.sh", "update-web.sh", "deploy-web.sh"):
        text = SCRIPTS[name].read_text()
        assert 'git config --system --fixed-value --unset-all safe.directory "$APP_DIR"' in text, name


def test_the_first_installs_downloads_go_in_a_folder_of_roots_own(tmp_path):
    text = DEPLOY_API.read_text()
    uv = text.split('step "Installing uv"', 1)[1].split("# ----", 1)[0]
    assert "/tmp/" not in uv and 'UV_TMP="$(mktemp -d)"' in uv
    assert "trap 'rm -rf -- \"$UV_TMP\"' EXIT" in uv and 'rm -rf -- "$UV_TMP"\n  trap - EXIT' in uv
    # Run it, the downloads stubbed: everything lands in the private folder, which is gone after.
    temp = tmp_path / "temp"
    temp.mkdir()
    _fake_bin(tmp_path, "curl", 'out=""; while [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && out="$2"; shift; done\n'
                                'mkdir -p x/uv-t && printf "uv" > x/uv-t/uv && printf "uvx" > x/uv-t/uvx\n'
                                'if [[ "$out" == *.sha256 ]]; then (cd "$(dirname "$out")" && sha256sum uv.tar.gz) '
                                '> "$out"; else tar -czf "$out" -C x uv-t; fi\n')
    _fake_bin(tmp_path, "install", f'echo "$@" >> {tmp_path}/installed\n')
    block = uv.split("\n", 1)[1].replace('ok "uv $($UV_BIN --version)"', "")
    script = ('ok() { :; }; fail() { echo "fail: $1"; exit 1; }\n'
              f'UV_BIN="{tmp_path}/uv"; RUST_TARGET=t; UV_VERSION=0.7.12\n' + block)
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30,
                         cwd=tmp_path,
                         env={**os.environ, "TMPDIR": str(temp), "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"})
    assert out.returncode == 0, out.stdout + out.stderr
    installed = (tmp_path / "installed").read_text().splitlines()
    assert [line.split()[2].startswith(str(temp)) for line in installed] == [True, True], installed
    assert list(temp.iterdir()) == [], "the folder is gone"


def test_root_reads_and_writes_nothing_in_the_service_users_home():
    # Its CLI config: read as it (a link there points where it can read, no further), written with its umask.
    deploy, update = DEPLOY_API.read_text(), UPDATE_API.read_text()
    for text in (deploy, update):
        assert "--from-cli-config -" in text and '--from-cli-config "' not in text
        assert "as_svc timeout 10 head -c 1048576 -- " in text
    tenant = deploy.split("# 12. Provision default tenant", 1)[1].split("# 13.", 1)[0]
    # Whether the first account is made, asked before the install's long steps too: account_made.
    made = deploy.split("account_made() {", 1)[1].split("\n}\n", 1)[0]
    assert 'sudo -u "${SVC_USER}" grep -qs ' in made and "if account_made; then" in tenant
    assert "grep -q '\"api_key\"" not in deploy and deploy.count("grep -qs '\"api_key\"") == 1
    assert 'sudo -u "${SVC_USER}" -H python3 -I -' in tenant
    assert '\nchmod 600 "$CLI_CONFIG"' not in tenant and 'sudo -u "${SVC_USER}" chmod 600 "$CLI_CONFIG"' in tenant
    assert '(umask 077 && sudo -u "${SVC_USER}" tee "$CLI_CONFIG"' in tenant
    # Its .python-version and venv's pyvenv.cfg, read as it too.
    assert 'HAVE_PY="$(as_svc sed ' in update and 'WANT_PY="$(as_svc grep ' in update


def test_the_carried_cli_config_reaches_the_env_file_as_single_lines_only(tmp_path):
    # update-api.sh's scheduler settings step, with sudo running what it's given as us: a value with a line break
    # in the service user's config is left out, never a line of the env file (which every unit reads).
    home = tmp_path / "home"
    (home / ".lumiverb").mkdir(parents=True)
    (home / ".lumiverb" / "config.json").write_text(
        '{"gpu_decodes": "1\\nDATA_DIR=/elsewhere", "render_concurrency": 2}')
    env = tmp_path / "env"
    env.write_text("API_PORT=8100\n")
    text = UPDATE_API.read_text()
    block = text.split('step "Scheduler settings"', 1)[1].split("# ----", 1)[0]
    script = (
        'ok() { echo "ok: $1"; }; warn() { echo "warn: $1"; }\n'
        'sudo() { shift 2; "$@"; }\n'
        f'ENV_FILE="{env}"; APP_DIR="{REPO}"; SVC_HOME="{home}"; SVC_USER=lumiverb; PROCESSING=true\n'
        + _update_helpers() + block)
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert env.read_text().splitlines() == ["API_PORT=8100", "LUMIVERB_RENDER_CONCURRENCY=2"]
    assert "gpu_decodes in the service user's CLI config isn't a whole number on one line: left out" in out.stderr
    (home / ".lumiverb" / "config.json").unlink()  # none at all: nothing carried, the update goes on
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr


def test_postgresql_lets_no_process_of_the_service_user_in_without_the_password():
    # Ubuntu's clusters let a local OS user in as the database role of its own name (peer). The service user's
    # name isn't the app's role, and the install adds nothing to pg_hba.conf or pg_ident.conf: a process of its
    # (a plug-in's) gets no further than one without the password.
    text = DEPLOY_API.read_text()
    assert 'SVC_USER="lumiverb"' in text and 'PG_USER="app"' in text
    for config in ("pg_hba", "pg_ident", "CREATE ROLE", "CREATE USER lumiverb", " trust"):
        assert config not in text, config
