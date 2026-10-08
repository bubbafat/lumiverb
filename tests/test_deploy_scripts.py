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


def test_web_on_its_own_machine_uses_defaults_and_flags(tmp_path):
    assert _web_settings(tmp_path)["BRANCH"] == "main"
    assert _web_settings(tmp_path)["NO_FIREWALL"] == "false"
    s = _web_settings(tmp_path, "--branch", "x", "--no-firewall", env_file=REMEMBERED.replace("BRANCH=feat/brain", "BRANCH=y"))
    assert s["BRANCH"] == "x"
    assert s["NO_FIREWALL"] == "true"


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
