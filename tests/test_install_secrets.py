"""Secrets at install and setup stay off command lines, which every local user
can see (ps) and shells keep in their history, and out of PostgreSQL's log.

- A role's password reaches PostgreSQL only as its SCRAM verifier, made on
  the client by libpq (psycopg2's encrypt_password): the Mac's role
  (src/server/lifecycle/roles.py) and Linux's (deploy-api.sh's own lines,
  tests/app_role.py), each set on a PostgreSQL that logs every statement it
  gets (log_statement=all). The role then signs in with the password.
- deploy-api.sh takes the first account's vision key from a file, from
  LUMIVERB_VISION_API_KEY or typed when asked; --vision-api-key still works,
  said against. The key reaches the API in JSON on curl's stdin.
- init.sh (a development checkout's) hands curl the admin key and the CLI the
  account's key, and writes a new admin key, never as an argument.
"""

from __future__ import annotations

import json
import os
import pty
import re
import secrets
import select
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import psycopg2
import pytest
from sqlalchemy import make_url

from src.server.lifecycle.roles import ensure_app_role
from tests.app_role import DEPLOY_API, make_app_role, role_step
from tests.conftest import PG_IMAGE, _RealPostgresContainer

REPO = Path(__file__).resolve().parents[1]
INIT_SH = REPO / "scripts" / "init.sh"


# --- A role's password, as PostgreSQL gets it ----------------------------------------------


@pytest.fixture(scope="module")
def logged():
    """A PostgreSQL of this module's own that logs every statement it's sent (to its container's output)."""
    with _RealPostgresContainer(PG_IMAGE).with_command("-c log_statement=all") as pg:
        yield pg


def _log(pg) -> str:
    """All the server has logged, up to a statement sent now: its lines reach the container's output a moment
    after it gets them, and a password looked for too soon would never be found."""
    marker = f"logged-{uuid.uuid4().hex}"
    url = make_url(pg.get_connection_url())
    conn = psycopg2.connect(host=url.host, port=url.port, user=url.username, password=url.password, dbname="postgres")
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT '{marker}'")
    finally:
        conn.close()
    deadline = time.monotonic() + 30
    while True:
        out, err = pg.get_logs()
        text = (out + err).decode(errors="replace")
        if marker in text:
            return text
        assert time.monotonic() < deadline, "the server's log never had the marker"
        time.sleep(0.1)


def _signs_in(pg, role: str, password: str) -> dict:
    url = make_url(pg.get_connection_url())
    conn = psycopg2.connect(host=url.host, port=url.port, user=role, password=password, dbname="postgres")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, rolcanlogin, rolcreatedb, rolsuper FROM pg_roles"
                        " WHERE rolname = current_user")
            user, login, createdb, superuser = cur.fetchone()
    finally:
        conn.close()
    return {"user": user, "login": login, "createdb": createdb, "superuser": superuser}


def _kept(pg, role: str) -> str:
    url = make_url(pg.get_connection_url())
    conn = psycopg2.connect(host=url.host, port=url.port, user=url.username, password=url.password, dbname="postgres")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,))
            return cur.fetchone()[0]
    finally:
        conn.close()


@pytest.mark.slow
def test_the_mac_s_role_password_reaches_postgresql_only_as_its_verifier(logged):
    password = "pw-" + secrets.token_urlsafe(24)
    superuser = logged.get_connection_url()
    app = make_url(superuser).set(username="lumiverb_app_t", password=password, database="control_t")
    for _ in range(2):  # made, then (every start) put right
        ensure_app_role(superuser, app.render_as_string(hide_password=False))
    log = _log(logged)
    assert password not in log
    # What it sent was logged: the role made and put right, with the verifier where the password was.
    for verb in ("CREATE", "ALTER"):
        assert re.search(rf"statement: {verb} ROLE lumiverb_app_t LOGIN CREATEDB [A-Z ]*PASSWORD 'SCRAM-SHA-256\$\d+:",
                         log), verb
    assert _kept(logged, "lumiverb_app_t").startswith("SCRAM-SHA-256$")
    assert _signs_in(logged, "lumiverb_app_t", password) == {
        "user": "lumiverb_app_t", "login": True, "createdb": True, "superuser": False}


@pytest.mark.slow
def test_linux_s_role_password_reaches_postgresql_only_as_its_verifier(logged, tmp_path):
    password = secrets.token_hex(24)  # as deploy-api.sh makes it (openssl rand -hex 24)
    said = []
    for _ in range(2):  # made, then (every run) put right
        said += make_app_role(logged.get_connection_url(), "app_t", password, tmp_path)
    log = _log(logged)
    assert password not in log
    for verb in ("CREATE", "ALTER"):
        assert re.search(rf"statement: {verb} USER \"app_t\" CREATEDB PASSWORD 'SCRAM-SHA-256\$\d+:", log), verb
    # Nor on a command line: su's and sudo's, which every local user sees.
    assert said and [line for line in said if password in line] == []
    assert _kept(logged, "app_t").startswith("SCRAM-SHA-256$")
    assert _signs_in(logged, "app_t", password) == {"user": "app_t", "login": True, "createdb": True,
                                                    "superuser": False}


@pytest.mark.fast
def test_the_install_has_libpq_s_python_before_its_role_step():
    text = DEPLOY_API.read_text()
    install = text.index("git build-essential python3-dev python3-psycopg2")
    assert install < text.index(role_step())


# --- deploy-api.sh's vision key -----------------------------------------------------------


def _vision_block() -> str:
    text = DEPLOY_API.read_text()
    function = text[text.index("account_made() {"):]
    function = function[:function.index("\n}\n") + 3]
    start = text.index("# The first account's vision key, never an argument\n")
    return function + text[start:text.index("# Before anything's made", start)]


def _vision_key(tmp_path: Path, *, url: str = "http://ollama.lan:11434/v1", key: str = "", key_file: str = "",
                environ: dict[str, str] | None = None, made: bool = False, answer: str | None = None
                ) -> tuple[int, str, str]:
    """deploy-api.sh's vision key section; (its exit status, the key it settled on, what it said). With an answer,
    it runs at a terminal, and the answer is typed once it asks."""
    cli_config = tmp_path / "config.json"
    cli_config.write_text('{"api_url": "http://127.0.0.1:8100", "api_key": "lv_k"}' if made else "{}")
    out = tmp_path / "key"
    leaked = tmp_path / "leaked"
    out.unlink(missing_ok=True)
    script = (
        'warn() { echo "warn: $1"; }; fail() { echo "fail: $1" >&2; exit 1; }\n'
        'id() { [[ -n "${MADE:-}" ]]; }; sudo() { shift 2; "$@"; }\n'
        f'SVC_USER=lumiverb; CLI_CONFIG="{cli_config}"\n'
        + _vision_block()
        + f'\nprintf "%s" "$VISION_API_KEY" > "{out}"\n'
        + f'env | grep "^LUMIVERB_VISION_API_KEY=" > "{leaked}" || true\n'
    )
    env = {"PATH": os.environ["PATH"], "VISION_API_URL": url, "VISION_API_KEY": key, "VISION_API_KEY_FILE": key_file,
           **({"MADE": "1"} if made else {}), **(environ or {})}
    if answer is None:
        done = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True,
                              timeout=30, env=env, stdin=subprocess.DEVNULL)
        code, said = done.returncode, done.stdout + done.stderr
    else:
        terminal, typed_at = pty.openpty()
        proc = subprocess.Popen(["bash", "-euo", "pipefail", "-c", script], stdin=typed_at,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        os.close(typed_at)
        asked = b""
        deadline = time.monotonic() + 30
        while b"Enter for none" not in asked and proc.poll() is None:
            assert time.monotonic() < deadline, asked
            if select.select([proc.stderr], [], [], 0.5)[0]:
                asked += os.read(proc.stderr.fileno(), 4096)
        if b"Enter for none" in asked:
            os.write(terminal, answer.encode() + b"\n")
        rest_out, rest_err = proc.communicate(timeout=30)
        os.close(terminal)
        code, said = proc.returncode, (rest_out + asked + rest_err).decode()
    if code == 0:
        assert leaked.read_text() == "", "what the install starts gets no key"
    return code, out.read_text() if out.exists() else "", said


@pytest.mark.fast
def test_the_vision_key_is_read_from_a_file_its_first_line(tmp_path):
    key_file = tmp_path / "vision.key"
    key_file.write_text('sk-from"a\\file\r\nsecond line\n')
    key_file.chmod(0o600)
    code, key, said = _vision_key(tmp_path, key_file=str(key_file))
    assert (code, key) == (0, 'sk-from"a\\file'), said
    # Beside LUMIVERB_VISION_API_KEY, the file's.
    assert _vision_key(tmp_path, key_file=str(key_file), environ={"LUMIVERB_VISION_API_KEY": "sk-env"})[1] \
        == 'sk-from"a\\file'


@pytest.mark.fast
@pytest.mark.parametrize("content", [None, "", "\nsk-on-the-second-line\n"], ids=["missing", "empty", "first-empty"])
def test_a_key_file_with_no_key_stops_the_install(tmp_path, content):
    key_file = tmp_path / "vision.key"
    if content is not None:
        key_file.write_text(content)
    code, _, said = _vision_key(tmp_path, key_file=str(key_file))
    assert code != 0
    assert f"fail: {key_file}" in said or f"fail: Can't read {key_file}" in said


@pytest.mark.fast
def test_the_vision_key_is_read_from_the_environment_and_kept_from_what_the_install_starts(tmp_path):
    code, key, said = _vision_key(tmp_path, environ={"LUMIVERB_VISION_API_KEY": "sk-env-1"})
    assert (code, key) == (0, "sk-env-1"), said  # _vision_key checks nothing it starts has it
    assert "Enter for none" not in said


@pytest.mark.fast
def test_the_vision_key_is_asked_for_at_a_terminal(tmp_path):
    code, key, said = _vision_key(tmp_path, answer="sk-typed-1")
    assert (code, key) == (0, "sk-typed-1"), said
    assert "The key for http://ollama.lan:11434/v1 (not shown; Enter for none): " in said
    assert "sk-typed-1" not in said, "not shown"
    # Enter alone: a machine with no key (Ollama's).
    assert _vision_key(tmp_path, answer="")[:2] == (0, "")


@pytest.mark.fast
def test_without_a_terminal_or_a_url_nothing_is_asked(tmp_path):
    for kwargs in ({}, {"url": ""}):
        code, key, said = _vision_key(tmp_path, **kwargs)
        assert (code, key) == (0, ""), said
        assert "Enter for none" not in said
    assert "Enter for none" not in _vision_key(tmp_path, url="", answer="sk")[2]


@pytest.mark.fast
def test_once_the_first_account_is_made_nothing_is_asked_and_the_url_is_said_unused(tmp_path):
    code, _, said = _vision_key(tmp_path, made=True, answer="sk-never")
    assert code == 0 and "Enter for none" not in said
    assert ("warn: The first account is made already, and --vision-api-url and its key are only for making it: "
            "add the machine in Settings › AI") in said
    # Nothing given, nothing said.
    assert "warn" not in _vision_key(tmp_path, url="", made=True)[2]


def _dry_run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(DEPLOY_API), "--app-host", "http://x", *args, "--dry-run"],
                          capture_output=True, text=True, timeout=30, check=False,
                          env={**os.environ, "LUMIVERB_CONF_DIR": str(tmp_path / "conf")})


@pytest.mark.fast
def test_vision_api_key_still_works_said_against(tmp_path):
    plain = _dry_run(tmp_path)
    flagged = _dry_run(tmp_path, "--vision-api-url", "http://gpu:11434/v1", "--vision-api-key", "sk-arg")
    assert flagged.returncode == 0, flagged.stderr
    assert flagged.stdout == plain.stdout, "a dry run's stdout is its settings alone"
    assert ("--vision-api-key puts the key on this command's line, which every local user can see (ps) while the "
            "install runs, and in your shell's history. Use --vision-api-key-file FILE or LUMIVERB_VISION_API_KEY "
            "instead, or leave it out to be asked for it.") in flagged.stderr
    assert "sk-arg" not in flagged.stdout + flagged.stderr
    # Taken as before: the key the section settles on.
    assert _vision_key(tmp_path, key="sk-arg")[:2] == (0, "sk-arg")


@pytest.mark.fast
def test_a_key_and_a_key_file_both_stop_the_install(tmp_path):
    both = _dry_run(tmp_path, "--vision-api-key", "sk", "--vision-api-key-file", str(tmp_path / "k"))
    assert both.returncode != 0
    assert "Give --vision-api-key-file or --vision-api-key, not both" in both.stderr


def _provision(tmp_path: Path, *, name: str, url: str, key: str) -> tuple[dict, list[str]]:
    """deploy-api.sh's first account, made by curl (a stand-in that keeps its arguments, its headers' files and
    stdin); the body it sent and its command line."""
    text = DEPLOY_API.read_text()
    start = text.index("ADMIN_KEY=\"$(grep -m1 '^ADMIN_KEY=' \"${ENV_FILE}\" | cut -d= -f2-)\"")
    end = text.index('ok "Tenant ${TENANT_ID} provisioned"', start)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "curl").write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/curl.args"\n'
        'for a in "$@"; do [[ "$a" == @/* ]] && cat "${a#@}" >> ' + f'"{tmp_path}/curl.headers"; done\n'
        f'cat > "{tmp_path}/curl.body"\n'
        """echo '{"tenant_id": "ten_1", "api_key": "lv_1"}'\n""")
    (bin_dir / "curl").chmod(0o755)
    env_file = tmp_path / "env"
    env_file.write_text("ADMIN_KEY=install-key-1\n")
    script = ('fail() { echo "fail: $1" >&2; exit 1; }\n'
              + text[start:end] + '\necho "$TENANT_ID $TENANT_API_KEY"\n')
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=30,
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "ENV_FILE": str(env_file),
                              "API_PORT": "8100", "TENANT_NAME": name, "CERTBOT_EMAIL": "you@example.org",
                              "VISION_API_URL": url, "VISION_API_KEY": key})
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip() == "ten_1 lv_1"
    assert (tmp_path / "curl.headers").read_text() == "Authorization: Bearer install-key-1\n"
    return json.loads((tmp_path / "curl.body").read_text()), (tmp_path / "curl.args").read_text().splitlines()


@pytest.mark.fast
def test_the_first_account_gets_the_vision_key_in_json_on_curl_s_stdin(tmp_path):
    body, args = _provision(tmp_path / "a", name='Robert\'s "photos"', url="http://gpu:11434/v1", key='sk-"odd\\key')
    assert body == {"name": 'Robert\'s "photos"', "email": "you@example.org", "vision_api_url": "http://gpu:11434/v1",
                    "vision_api_key": 'sk-"odd\\key'}
    assert [arg for arg in args if "sk-" in arg or "install-key-1" in arg] == []
    # Without a vision machine, none.
    assert _provision(tmp_path / "b", name="Lumiverb", url="", key="")[0] == {"name": "Lumiverb",
                                                                              "email": "you@example.org"}


# --- init.sh (a development checkout's) ---------------------------------------------------

# Every program init.sh starts but bash's own: each keeps its command line, then the real one runs (or a stand-in
# for what a test can't have: Docker, uv, the API, the CLI).
REAL_TOOLS = ("python3", "grep", "sed", "tr", "head", "tail")
STANDINS = {
    "docker": 'if [[ "$1" == compose ]]; then echo \'{"Name": "lumiverb-postgres", "State": "running"}\'; fi\n',
    "uv": "exit 0\n",
    "curl": (
        'for a in "$@"; do [[ "$a" == @/* ]] && cat "${a#@}" >> "$SEEN/curl.headers"; done\n'
        'case "$*" in\n'
        '  *"-X POST"*) printf \'{"tenant_id": "ten_1", "api_key": "lv-account-key-1"}\\n201\\n\' ;;\n'
        '  */health*) exit 0 ;;\n'
        '  *) echo "[]" ;;\n'
        'esac\n'),
    "lumiverb": 'cat > "$SEEN/lumiverb.stdin"\n',
}


@pytest.mark.fast
def test_init_sh_hands_its_keys_to_no_program_as_an_argument(tmp_path):
    seen, bin_dir, checkout = tmp_path / "seen", tmp_path / "bin", tmp_path / "checkout"
    for folder in (seen, bin_dir, checkout):
        folder.mkdir()
    keep = f'printf "%s\\n" "$(basename "$0") $*" >> "{seen}/argv"\n'
    for tool in REAL_TOOLS:
        (bin_dir / tool).write_text(f'#!/bin/bash\n{keep}exec "{shutil.which(tool)}" "$@"\n')
    for tool, body in STANDINS.items():
        (bin_dir / tool).write_text(f"#!/bin/bash\n{keep}{body}")
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    (checkout / ".env.local").write_text("JWT_SECRET=j\nADMIN_KEY=\nAPP_ENV=development\n")
    out = subprocess.run(["bash", str(INIT_SH)], cwd=checkout, capture_output=True, text=True, timeout=60,
                         env={"PATH": f"{bin_dir}:/usr/bin:/bin", "SEEN": str(seen), "HOME": str(tmp_path)})
    assert out.returncode == 0, out.stdout + out.stderr

    lines = (checkout / ".env.local").read_text().splitlines()
    admin_key = lines[1].removeprefix("ADMIN_KEY=")
    assert re.fullmatch(r"[0-9a-f]{64}", admin_key) and lines[::2] == ["JWT_SECRET=j", "APP_ENV=development"]
    argv = (seen / "argv").read_text()
    assert "curl " in argv and "lumiverb config set" in argv and "python3 " in argv
    assert admin_key not in argv and "lv-account-key-1" not in argv
    assert (seen / "curl.headers").read_text() == f"Authorization: Bearer {admin_key}\n" * 2
    assert "lumiverb config set --api-url http://localhost:8000 --api-key -\n" in argv
    assert (seen / "lumiverb.stdin").read_text() == "lv-account-key-1\n"
