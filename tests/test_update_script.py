"""scripts/update.sh: one command that updates the whole install, logged.

Runs against a throwaway git remote and checkout, with sudo, systemctl,
curl, chown and `id -u` stubbed, and update-api.sh / update-web.sh replaced
by stand-ins that say they ran. LUMIVERB_APP_DIR, LUMIVERB_CONF_DIR,
LUMIVERB_LOG_DIR and LUMIVERB_NGINX_SITE stand in for /opt/lumiverb,
/etc/lumiverb, /var/log/lumiverb and the nginx site.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parents[1]
UPDATE = REPO / "scripts" / "update.sh"

STUBS = {
    "id": 'if [[ "${1:-}" == -u ]]; then echo 0; else exec /usr/bin/id "$@"; fi',
    # sudo -u USER [-H] [--preserve-env=...] cmd...: run cmd as us.
    "sudo": 'while [[ "${1:-}" == -* ]]; do [[ "$1" == -u ]] && shift; shift; done; exec "$@"',
    "systemctl": (
        'echo "systemctl $*" >> "$CALLS"\n'
        'unit="${@: -1}"\n'
        'case "$1" in\n'
        '  is-enabled) exit 0 ;;\n'
        '  is-active) if [[ " ${INACTIVE:-} " == *" $unit "* ]]; then echo inactive; exit 3; fi; echo active ;;\n'
        "esac"
    ),
    "curl": 'echo "curl $*" >> "$CALLS"; [[ -z "${API_DOWN:-}" ]]',
    "chown": 'echo "chown $*" >> "$CALLS"',
}


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                         env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
    return out.stdout.strip()


@pytest.fixture
def install(tmp_path: Path):
    """An origin with main and feat/brain, and an install checked out on feat/brain."""
    seed = tmp_path / "seed"
    (seed / "scripts").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "scripts" / "update.sh").write_text(UPDATE.read_text())
    (seed / "scripts" / "update-api.sh").write_text('echo "update-api ran"; exit "${FAKE_API_RC:-0}"\n')
    (seed / "scripts" / "update-web.sh").write_text('echo "update-web ran"\n')
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "first")
    _git(seed, "branch", "feat/brain")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    app = tmp_path / "app"
    _git(tmp_path, "clone", "-q", "-b", "feat/brain", str(origin), str(app))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text(f"#!/usr/bin/env bash\n{body}\n")
        stub.chmod(0o755)
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "env").write_text("API_PORT=8100\nBRANCH=feat/brain\nADMIN_KEY=k-7f3a-not-for-logs\n")
    site = tmp_path / "nginx-site"
    site.write_text("server {}\n")
    calls = tmp_path / "calls"
    calls.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "LUMIVERB_APP_DIR": str(app),
        "LUMIVERB_CONF_DIR": str(conf),
        "LUMIVERB_LOG_DIR": str(tmp_path / "log"),
        "LUMIVERB_NGINX_SITE": str(site),
        "SUDO_USER": "robert",
        "CALLS": str(calls),
        "LUMIVERB_UPDATE_LOCK": str(tmp_path / "update.lock"),
        "LUMIVERB_UPDATE_SETTLE": "0",
    }
    for leftover in ("LUMIVERB_UPDATE_LOG", "LUMIVERB_UPDATE_BEFORE", "LUMIVERB_UPDATE_BRANCH"):
        env.pop(leftover, None)
    return {"tmp": tmp_path, "seed": seed, "app": app, "conf": conf, "site": site, "calls": calls, "env": env}


def _push(install, path: str, text: str, branch: str = "main") -> str:
    seed = install["seed"]
    _git(seed, "checkout", "-q", branch)
    (seed / path).write_text(text)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", f"change {path}")
    _git(seed, "push", "-q", str(install["tmp"] / "origin.git"), branch)
    return _git(seed, "rev-parse", "--short", "HEAD")


def _run(install, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(install["app"] / "scripts" / "update.sh"), *args],
        capture_output=True, text=True, timeout=60, env={**install["env"], **env},
    )


def _log(install) -> str:
    return (install["tmp"] / "log" / "update-latest.log").read_text()


def test_one_command_updates_the_api_then_the_web_and_logs_it(install):
    after = _push(install, "README", "new\n", branch="feat/brain")
    out = _run(install)
    assert out.returncode == 0, out.stdout + out.stderr
    log = _log(install)
    assert log.index("update-api ran") < log.index("update-web ran")
    assert "Result: OK" in log
    assert f"{after} change README" in log  # what came in
    assert _git(install["app"], "rev-parse", "--short", "HEAD") == after
    # The screen gets what the log gets.
    assert "Result: OK" in out.stdout and "update-web ran" in out.stdout


def test_the_log_is_the_invoking_user_s_and_latest_points_at_it(install):
    _run(install)
    logs = sorted((install["tmp"] / "log").glob("update-2*.log"))
    assert len(logs) == 1
    assert (install["tmp"] / "log" / "update-latest.log").resolve() == logs[0].resolve()
    assert f"chown -h robert {logs[0]}" in install["calls"].read_text()
    assert oct(logs[0].stat().st_mode & 0o777) == "0o640"


def test_the_log_keeps_secrets_out(install):
    _run(install)
    assert "k-7f3a-not-for-logs" not in _log(install)


def test_branch_switches_the_install_and_is_remembered(install):
    after = _push(install, "README", "main only\n", branch="main")
    out = _run(install, "--branch", "main")
    assert out.returncode == 0, out.stdout + out.stderr
    assert _git(install["app"], "branch", "--show-current") == "main"
    assert _git(install["app"], "rev-parse", "--short", "HEAD") == after
    env_text = (install["conf"] / "env").read_text()
    assert "BRANCH=main\n" in env_text and "feat/brain" not in env_text
    assert "ADMIN_KEY=k-7f3a-not-for-logs" in env_text  # the rest of the file stays
    # The next run stays on main without being told.
    _push(install, "README", "again\n", branch="main")
    assert _run(install).returncode == 0
    assert _git(install["app"], "branch", "--show-current") == "main"


def test_without_branch_the_checkout_s_branch_is_kept_and_remembered(install):
    (install["conf"] / "env").write_text("API_PORT=8100\n")
    assert _run(install).returncode == 0
    assert "BRANCH=feat/brain" in (install["conf"] / "env").read_text()


def test_the_pulled_copy_of_the_script_runs(install):
    # bash runs the copy it started with: a step the update adds must run now.
    new = UPDATE.read_text().replace("set -euo pipefail\n", 'set -euo pipefail\necho "pulled copy"\n', 1)
    _push(install, "scripts/update.sh", new, branch="feat/brain")
    out = _run(install)
    assert out.returncode == 0, out.stdout + out.stderr
    assert _log(install).count("pulled copy") == 1


def test_a_failed_api_update_stops_before_the_web_and_says_so(install):
    out = _run(install, FAKE_API_RC="3")
    assert out.returncode != 0
    log = _log(install)
    assert "update-api ran" in log
    assert "update-web ran" not in log
    assert "Result: FAILED" in log and "Result: OK" not in log


def test_a_service_not_running_afterwards_fails_the_update(install):
    out = _run(install, INACTIVE="lumiverb-scheduler")
    assert out.returncode != 0
    log = _log(install)
    assert "lumiverb-scheduler: inactive" in log
    assert "Result: FAILED" in log


def test_an_api_that_doesn_t_answer_fails_the_update(install):
    out = _run(install, API_DOWN="1")
    assert out.returncode != 0
    assert "Result: FAILED" in _log(install)
    calls = install["calls"].read_text()
    assert "127.0.0.1:8100/health" in calls and "--max-time" in calls


def test_no_nginx_site_skips_the_web_ui(install):
    install["site"].unlink()
    out = _run(install)
    assert out.returncode == 0, out.stdout + out.stderr
    log = _log(install)
    assert "update-web ran" not in log
    assert "skipped" in log
    assert "Result: OK" in log


def test_a_checkout_with_local_commits_stops_instead_of_merging(install):
    app = install["app"]
    _git(app, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "local")
    _push(install, "README", "upstream\n", branch="feat/brain")
    out = _run(install)
    assert out.returncode != 0
    assert "Result: FAILED" in _log(install)
    assert "update-api ran" not in _log(install)


def test_back_to_back_runs_keep_a_log_each(install):
    assert _run(install).returncode == 0
    after = _push(install, "README", "second\n", branch="feat/brain")
    assert _run(install).returncode == 0
    logs = list((install["tmp"] / "log").glob("update-2*.log"))
    assert len(logs) == 2
    assert f"After:  {after} change README" in _log(install)


def test_old_logs_are_pruned(install):
    log_dir = install["tmp"] / "log"
    log_dir.mkdir()
    for i in range(25):
        old = log_dir / f"update-20200101-0000{i:02d}-1.log"
        old.write_text("old\n")
        os.utime(old, (1_600_000_000 + i, 1_600_000_000 + i))
    _run(install)
    assert len(list(log_dir.glob("update-2*.log"))) == 20


def test_without_sudo_it_says_so(install):
    env = {**install["env"], "PATH": os.environ["PATH"]}
    out = subprocess.run(["bash", str(install["app"] / "scripts" / "update.sh")],
                         capture_output=True, text=True, timeout=30, env=env)
    if os.geteuid() == 0:
        pytest.skip("running as root")
    assert out.returncode != 0
    assert "sudo" in out.stdout + out.stderr


def _lines(install) -> list[str]:
    import re

    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in _log(install).splitlines()]


def test_the_result_is_a_plain_line(install):
    assert _run(install).returncode == 0
    assert "Result: OK" in _lines(install)


def test_ctrl_c_during_the_update_says_it_failed(install):
    # Ctrl-C reaches the script while update-api.sh runs: it must not end "OK".
    _push(install, "scripts/update-api.sh", 'kill -INT "$PPID"; sleep 0.3; echo "api end"\n', branch="feat/brain")
    out = _run(install)
    assert out.returncode != 0
    lines = _lines(install)
    assert "Result: FAILED" in lines and "Result: OK" not in lines
    assert "update-web ran" not in _log(install)


def test_a_terminated_update_still_logs_its_result(install):
    # SIGTERM (or a hangup) to the whole process group, tee included.
    _push(install, "scripts/update-api.sh", "kill -TERM 0; sleep 1\n", branch="feat/brain")
    out = subprocess.run(["bash", str(install["app"] / "scripts" / "update.sh")], capture_output=True, text=True,
                         timeout=60, env=install["env"], start_new_session=True)
    assert out.returncode != 0
    assert "Result: FAILED" in _lines(install)


def test_a_branch_without_update_sh_changes_nothing(install):
    seed = install["seed"]
    _git(seed, "checkout", "-q", "-b", "old", "main")
    _git(seed, "rm", "-q", "scripts/update.sh")
    _git(seed, "commit", "-qm", "before update.sh")
    _git(seed, "push", "-q", str(install["tmp"] / "origin.git"), "old")
    out = _run(install, "--branch", "old")
    assert out.returncode != 0
    assert "update.sh" in out.stdout + out.stderr
    assert _git(install["app"], "branch", "--show-current") == "feat/brain"
    assert "BRANCH=feat/brain" in (install["conf"] / "env").read_text()
    assert "update-api ran" not in _log(install)
    assert "Result: FAILED" in _lines(install)


def test_two_updates_cant_run_at_once(install):
    import fcntl

    with open(install["env"]["LUMIVERB_UPDATE_LOCK"], "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = _run(install)
    assert out.returncode != 0
    assert "already running" in out.stdout + out.stderr
    assert not list((install["tmp"] / "log").glob("update-2*.log"))  # the running one's log stays the latest


def test_without_sudo_the_log_is_root_s_alone(install):
    env = {k: v for k, v in install["env"].items() if k != "SUDO_USER"}
    out = subprocess.run(["bash", str(install["app"] / "scripts" / "update.sh")], capture_output=True, text=True,
                         timeout=60, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    [log] = (install["tmp"] / "log").glob("update-2*.log")
    assert oct(log.stat().st_mode & 0o777) == "0o600"


def test_a_branch_name_git_allows_is_remembered_exactly(install):
    seed = install["seed"]
    _git(seed, "checkout", "-q", "-b", "fix&co|1", "main")
    _git(seed, "push", "-q", str(install["tmp"] / "origin.git"), "fix&co|1")
    out = _run(install, "--branch", "fix&co|1")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "BRANCH=fix&co|1\n" in (install["conf"] / "env").read_text()


def test_a_name_that_isnt_a_branch_changes_nothing(install):
    out = _run(install, "--branch", "no such branch")
    assert out.returncode != 0
    assert _git(install["app"], "branch", "--show-current") == "feat/brain"


def test_moving_to_an_older_commit_says_so(install):
    _push(install, "README", "newer\n", branch="feat/brain")
    assert _run(install).returncode == 0
    out = _run(install, "--branch", "main")  # main is behind feat/brain now
    assert out.returncode == 0, out.stdout + out.stderr
    assert any("No new commits: moved to an older one, 1 back" in line for line in _lines(install))


def test_the_first_run_can_come_from_the_new_branch_itself(install):
    """The bootstrap: the install's branch predates update.sh, so the script
    is read from the branch it's moving to (`git show ... | bash -s`)."""
    app, seed = install["app"], install["seed"]
    _git(seed, "checkout", "-q", "-b", "legacy", "main")
    _git(seed, "rm", "-q", "scripts/update.sh")
    _git(seed, "commit", "-qm", "legacy tip")
    _git(seed, "push", "-q", str(install["tmp"] / "origin.git"), "legacy")
    _git(app, "fetch", "-q", "origin")
    _git(app, "checkout", "-q", "-b", "legacy", "origin/legacy")
    out = subprocess.run(["bash", "-s", "--", "--branch", "main"], input=UPDATE.read_text(), capture_output=True,
                         text=True, timeout=60, env=install["env"])
    assert out.returncode == 0, out.stdout + out.stderr
    assert _git(app, "branch", "--show-current") == "main"
    lines = _lines(install)
    assert any(line.strip().startswith("Before:") and "legacy tip" in line for line in lines)
    assert "Result: OK" in lines


def test_ctrl_c_in_the_terminal_still_logs_that_it_failed(install):
    # A terminal's Ctrl-C reaches every process in the group: the script,
    # update-api.sh and tee alike.
    _push(install, "scripts/update-api.sh", "kill -INT 0; sleep 1\n", branch="feat/brain")
    out = subprocess.run(["bash", str(install["app"] / "scripts" / "update.sh")], capture_output=True, text=True,
                         timeout=60, env=install["env"], start_new_session=True)
    assert out.returncode != 0
    lines = _lines(install)
    assert "Result: FAILED" in lines and "Result: OK" not in lines


def test_the_scheduler_stops_before_the_pull(install):
    """Review: the old scheduler ran on between the pull and update-api.sh's
    stop, on files the pull had moved. update-api.sh starts it again."""
    text = UPDATE.read_text()
    stop = text.index("systemctl stop lumiverb-scheduler")
    assert stop < text.index('as_svc git checkout "$BRANCH"') < text.index("as_svc git pull")
    out = _run(install)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "systemctl stop lumiverb-scheduler" in install["calls"].read_text()
    assert "The scheduler is stopped" not in _log(install)


def test_a_failure_with_the_scheduler_stopped_says_how_to_start_it(install):
    app = install["app"]
    _git(app, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "local")
    _push(install, "README", "upstream\n", branch="feat/brain")
    out = _run(install, INACTIVE="lumiverb-scheduler")  # stopped, then the pull fails
    assert out.returncode != 0
    lines = _lines(install)
    said = next(i for i, line in enumerate(lines) if "The scheduler is stopped" in line)
    assert "sudo systemctl start lumiverb-scheduler" in lines[said]
    assert lines.index("Result: FAILED") > said
    # Not started for you: the code may be half-deployed.
    assert "systemctl start lumiverb-scheduler" not in install["calls"].read_text()
