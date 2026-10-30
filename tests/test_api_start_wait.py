"""update-api.sh waits for the API it restarted: a first start after new code compiles it, ~12 s on a 2-vCPU droplet
(Oct 30), which a 10-second wait failed. Its wait, run as the script has it with curl, systemctl and sleep stubbed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

SCRIPT = Path(__file__).parent.parent / "scripts" / "update-api.sh"


def _wait(tmp_path: Path, *, answers_after: int, fails_after: int | None = None) -> subprocess.CompletedProcess:
    """The script's wait, the API answering after `answers_after` tries (or its unit failing after `fails_after`)."""
    text = SCRIPT.read_text()
    start = text.index("ANSWERS=false")
    end = text.index("\n", text.index('[[ "$ANSWERS" == true ]]'))
    stubs = f"""
set -euo pipefail
API_PORT=8100
TRIES=0
fail() {{ echo "FAILED: $*"; exit 1; }}
sleep() {{ :; }}
curl() {{ TRIES=$((TRIES + 1)); (( TRIES > {answers_after} )); }}
systemctl() {{ {"(( TRIES >= " + str(fails_after) + " ))" if fails_after is not None else "false"}; }}
"""
    script = tmp_path / "wait.sh"
    script.write_text(stubs + text[start:end] + '\necho "answered after $TRIES"\n')
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30)


def test_an_api_that_takes_a_cold_start_is_waited_for(tmp_path):
    done = _wait(tmp_path, answers_after=15)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "answered after 16" in done.stdout
    assert "Waiting for the API to start (10 s)" in done.stdout


def test_an_api_that_never_answers_fails_after_90_s(tmp_path):
    done = _wait(tmp_path, answers_after=10_000)
    assert done.returncode == 1
    assert "FAILED: API server not responding after 90 s" in done.stdout


def test_a_unit_that_fails_stops_the_wait_at_once(tmp_path):
    done = _wait(tmp_path, answers_after=10_000, fails_after=3)
    assert done.returncode == 1
    assert "FAILED: API server not responding after 3 s" in done.stdout
