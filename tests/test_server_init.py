"""lumiverb-server init (src/server/lifecycle/init.py): an install's env file
and nginx's config, each written once. Homebrew runs it on every upgrade, so
what's there is kept, and what it makes agrees with the env file. The env
file points at Lumiverb's own PostgreSQL and names what `lumiverb-server run`
starts. nginx's server block is Linux's script's own, to the byte, and the
env file names every setting Linux's does. Nothing of Quickwit's (ADR-019:
search is in PostgreSQL), even for an older env file that names it."""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from src.server.lifecycle import cli
from src.server.lifecycle.envfile import read_env_file
from src.server.lifecycle.init import (
    Choices,
    InitError,
    init,
    nginx_conf,
    nginx_site,
)
from src.server.lifecycle.migrate import APP_DIR

pytestmark = pytest.mark.fast

SCRIPTS = APP_DIR / "scripts"
OPT = Path("/opt/homebrew/opt")


@pytest.fixture(autouse=True)
def no_install_in_the_environment(monkeypatch):
    monkeypatch.delenv("LUMIVERB_ENV_FILE", raising=False)
    monkeypatch.delenv("CONTROL_PLANE_DATABASE_URL", raising=False)


def _counter():
    count = iter(range(1, 100))
    return lambda: f"secret-{next(count)}"


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {"env": tmp_path / "etc" / "env", "nginx": tmp_path / "etc" / "nginx.conf"}


def _homebrews(tmp_path: Path) -> Choices:
    """What the formula's post_install says."""
    paths = _paths(tmp_path)
    return Choices(data_dir=tmp_path / "var", pg_bin=OPT / "postgresql@18/bin", nginx_bin=OPT / "nginx/bin/nginx", nginx_config=paths["nginx"], local_roots=True,
                   reset_on_this_machine=True)


def _install(tmp_path: Path, choices: Choices | None = None, said: list[str] | None = None) -> dict[str, Path]:
    paths = _paths(tmp_path)
    init(paths["env"], choices or _homebrews(tmp_path), web_root=tmp_path / "web",
         say=(said if said is not None else []).append, secret=_counter())
    return paths


def _hand_made(tmp_path: Path, text: str) -> Path:
    env_file = _paths(tmp_path)["env"]
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(text)
    return env_file


def test_a_new_install_gets_both_and_they_agree(tmp_path):
    said: list[str] = []
    paths = _install(tmp_path, said=said)
    env = read_env_file(paths["env"])

    assert stat.S_IMODE(paths["env"].stat().st_mode) == 0o600, "secrets: the owner's only"
    # Lumiverb's own PostgreSQL: its role, a password of its own, a port clear of Homebrew's 5432.
    assert env["CONTROL_PLANE_DATABASE_URL"] == "postgresql+psycopg2://lumiverb:secret-4@127.0.0.1:6468/control_plane"
    assert env["TENANT_DATABASE_URL_TEMPLATE"] == "postgresql+psycopg2://lumiverb:secret-4@127.0.0.1:6468/{tenant_id}"
    assert [env["ADMIN_KEY"], env["API_SECRET_KEY"], env["JWT_SECRET"]] == ["secret-1", "secret-2", "secret-3"]
    assert env["DATA_DIR"] == str(tmp_path / "var")
    # Devices: 6464, and 6465 for HTTPS (Settings › Network changes them); the API on this machine alone, 6364.
    assert (env["WEB_PORT"], env["WEB_TLS_PORT"], env["API_PORT"]) == ("6464", "6465", "6364")
    assert env["APP_HOST"] == "http://localhost:6464"
    # What run starts besides the API and the scheduler: no search service (ADR-019).
    assert {key: env[key] for key in ("PG_BIN", "NGINX_BIN", "NGINX_CONFIG")} == {
        "PG_BIN": str(OPT / "postgresql@18/bin"), "NGINX_BIN": str(OPT / "nginx/bin/nginx"),
        "NGINX_CONFIG": str(paths["nginx"])}
    assert not [key for key in env if key.startswith("QUICKWIT")]

    # The Mac has the files: the scheduler reads each library folder where it is, said here.
    assert env["LUMIVERB_ROOT_MAP"] == '{"/": "/"}'
    assert "# Library folders: the scheduler reads each one where it is" in paths["env"].read_text()
    # Whoever is at the Mac owns it: Forgot password there gives the link at once, said here.
    assert env["LUMIVERB_RESET_ON_THIS_MACHINE"] == "true"
    assert "# Forgot password, on this machine itself: the reset link at once" in paths["env"].read_text()

    conf = paths["nginx"].read_text()
    # A whole config of its own: in the foreground, errors to the service's log.
    assert conf.startswith("# Lumiverb's own nginx") and "\ndaemon off;\nerror_log stderr;\n" in conf
    assert "        application/javascript js mjs;\n" in conf and "\n    access_log off;\n" in conf
    # Where it listens is Settings › Network's: this machine only, until an admin
    # opens it up (it's set up here first). run rewrites the lines it includes.
    assert f"        include {tmp_path / 'var' / 'nginx' / 'listen.conf'};\n" in conf
    assert "listen" not in conf.replace("listen.conf", ""), "nothing fixed here"
    listen = (tmp_path / "var" / "nginx" / "listen.conf").read_text()
    assert "listen 127.0.0.1:6464 default_server;\nlisten [::1]:6464 default_server;\n" in listen
    assert "listen 6464" not in listen, "not on every address"
    assert "proxy_pass http://127.0.0.1:6364;" in conf
    assert f"root {tmp_path / 'web'};" in conf

    for folder in ("cache", "tmp", "worker-tmp", "nginx"):
        assert (tmp_path / "var" / folder).is_dir()
    assert not (tmp_path / "var" / "quickwit").exists()
    assert said == [f"✓ {paths['env']}", f"✓ {paths['nginx']}"]


def test_nginx_s_config_is_linux_s_server_block_inside_it():
    site = nginx_site(listen=("listen 127.0.0.1:6464 default_server;",), server_name="_", root=Path("/web"),
                      upstream="http://127.0.0.1:6465")
    conf = nginx_conf(site)
    assert "\n".join(f"    {line}" if line else "" for line in site.splitlines()) in conf
    assert conf.endswith("    }\n}\n")


def test_every_upgrade_keeps_what_is_there(tmp_path):
    paths = _install(tmp_path)
    before = {name: path.read_bytes() for name, path in paths.items()}
    said: list[str] = []
    _install(tmp_path, said=said)  # Homebrew's post_install, again
    assert {name: path.read_bytes() for name, path in paths.items()} == before, "the secrets above all"
    assert said == [f"· {paths['env']} kept", f"· {paths['nginx']} kept"]


def test_an_upgrade_leaves_run_s_listen_lines_alone(tmp_path):
    """Homebrew runs init after an upgrade's link, as the new run starts:
    the lines are run's (with the certificate other devices are served),
    and init only makes them for a new install."""
    _install(tmp_path)
    lines = tmp_path / "var" / "nginx" / "listen.conf"
    run_s = "listen 6464 default_server;\nlisten 6443 ssl default_server;\nssl_certificate /x;\n"
    lines.write_text(run_s)
    _install(tmp_path)  # Homebrew's post_install, again
    assert lines.read_text() == run_s


def test_the_others_follow_an_env_file_edited_by_hand(tmp_path):
    paths = _paths(tmp_path)
    # An older install's: it still names Quickwit, which nothing writes for now.
    quickwit = tmp_path / "etc" / "quickwit.yaml"
    env_file = _hand_made(tmp_path, f"DATA_DIR={tmp_path / 'elsewhere'}\nAPI_PORT=9100\nQUICKWIT_PORT=9200\n"
                                    f"WEB_PORT=9300\nQUICKWIT_CONFIG={quickwit}\n"
                                    f"NGINX_CONFIG={paths['nginx']}\n")
    _install(tmp_path, Choices())

    assert env_file.read_text().startswith("DATA_DIR="), "kept as it was"
    assert not quickwit.exists() and not (tmp_path / "elsewhere" / "quickwit").exists()
    conf = paths["nginx"].read_text()
    assert f"include {tmp_path / 'elsewhere' / 'nginx' / 'listen.conf'};" in conf
    assert "proxy_pass http://127.0.0.1:9100;" in conf
    assert "listen 127.0.0.1:9300 default_server;" in (tmp_path / "elsewhere" / "nginx" / "listen.conf").read_text()
    assert (tmp_path / "elsewhere" / "worker-tmp").is_dir()


def test_what_it_doesnt_name_isnt_written(tmp_path):
    paths = _install(tmp_path, Choices(data_dir=tmp_path / "var"))
    env = read_env_file(paths["env"])
    assert not {"PG_BIN", "NGINX_BIN", "NGINX_CONFIG", "LUMIVERB_ROOT_MAP",
                "LUMIVERB_RESET_ON_THIS_MACHINE"} & env.keys()
    assert not paths["nginx"].exists()


def test_a_choice_the_env_file_disagrees_with_is_refused_and_nothing_written(tmp_path):
    paths = _install(tmp_path)
    paths["nginx"].unlink()
    with pytest.raises(InitError, match=r"says API_PORT='6364' \(you said '7000'\).*edit it, or move it away"):
        _install(tmp_path, Choices(data_dir=tmp_path / "var", api_port=7000))
    with pytest.raises(InitError, match=r"says NGINX_BIN="):
        _install(tmp_path, Choices(nginx_bin=Path("/elsewhere/nginx")))
    assert not paths["nginx"].exists()


def test_a_new_env_file_needs_its_data_dir(tmp_path):
    with pytest.raises(InitError, match=r"^Say where its data goes: --data-dir DIR$"):
        _install(tmp_path, Choices())
    assert not (tmp_path / "etc" / "env").exists()


def test_an_env_file_with_a_data_dir_that_isnt_a_full_path_is_refused(tmp_path, monkeypatch):
    _hand_made(tmp_path, f"DATA_DIR=var\nAPI_PORT=6465\nWEB_PORT=6464\nNGINX_CONFIG={_paths(tmp_path)['nginx']}\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(InitError, match=r"^DATA_DIR must be a full path, not var$"):
        _install(tmp_path, Choices())
    assert not (tmp_path / "var").exists() and not _paths(tmp_path)["nginx"].exists()


def test_an_env_file_without_a_port_it_needs_says_which(tmp_path):
    env_file = _hand_made(tmp_path, f"DATA_DIR={tmp_path / 'var'}\nAPI_PORT=8100\n"
                                    f"NGINX_CONFIG={_paths(tmp_path)['nginx']}\n")
    with pytest.raises(InitError, match=rf"^{re.escape(str(env_file))} has no WEB_PORT$"):
        _install(tmp_path, Choices())


def _heredoc(script: Path, opener: str, variables: dict[str, str], *, also: tuple[str, ...] = ()) -> str:
    """What the script's heredoc writes, rendered by bash with these variables
    (and the script's own lines starting with `also`, e.g. its CSP)."""
    lines = script.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(opener))
    end = opener.rsplit("<<", 1)[1]
    body = lines[start + 1:lines.index(end, start + 1)]
    setup = [f"{key}={shlex.quote(value)}" for key, value in variables.items()]
    setup += [line for line in lines if line.startswith(also)] if also else []
    program = "\n".join([*setup, f"cat <<{end}", *body, end, ""])
    return subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=True).stdout


def test_the_nginx_site_is_deploy_web_s_to_the_byte():
    linux = _heredoc(SCRIPTS / "deploy-web.sh", "cat > /etc/nginx/sites-available/lumiverb <<NGINX",
                     {"LISTEN": "listen 6464 default_server;", "DOMAIN": "_", "APP_DIR": "/opt/lumiverb",
                      "API_UPSTREAM": "http://127.0.0.1:6465"}, also=("CSP=",))
    assert nginx_site(listen=("listen 6464 default_server;",), server_name="_",
                      root=Path("/opt/lumiverb/src/ui/web/dist"), upstream="http://127.0.0.1:6465",
                      logged=True) == linux, "its access log naming the site: a Linux server's"


def test_the_env_file_names_every_setting_linux_s_does(tmp_path):
    linux = _heredoc(SCRIPTS / "deploy-api.sh", 'cat > "$ENV_FILE" <<ENVEOF', {})
    keys = lambda text: {m.group(1) for m in re.finditer(r"^#? ?([A-Z_]+)=", text, re.MULTILINE)}  # noqa: E731
    # Its Postgres cluster (a Mac's port is in the database URL), checkout and ufw.
    linux_only = {"PG_PORT", "PG_VERSION", "BRANCH", "NO_FIREWALL"}
    # What run starts: a Linux install's systemd units name theirs.
    mac_only = {"WEB_PORT", "WEB_TLS_PORT", "PG_BIN", "NGINX_BIN", "NGINX_CONFIG",
                "LUMIVERB_ROOT_MAP",  # a Linux install's map is scheduler-env.py's, after this file
                "LUMIVERB_RESET_ON_THIS_MACHINE"}  # never a server's: anything on it could reset any account
    ours = keys(_install(tmp_path)["env"].read_text())
    assert ours - mac_only == keys(linux) - linux_only
    assert not [key for key in keys(linux) if key.startswith("QUICKWIT")], "neither names Quickwit (ADR-019)"


def test_the_command_writes_them_with_env_before_or_after(tmp_path, capsys):
    env_file = tmp_path / "env"
    assert cli.main(["init", "--env", str(env_file), "--data-dir", str(tmp_path / "var"),
                     "--pg-bin", str(OPT / "postgresql@18/bin"),
                     "--nginx-bin", str(OPT / "nginx/bin/nginx"),
                     "--nginx-config", str(tmp_path / "nginx.conf"), "--local-roots",
                     "--reset-on-this-machine"]) == 0
    env = read_env_file(env_file)
    assert env["DATA_DIR"] == str(tmp_path / "var") and env["NGINX_CONFIG"] == str(tmp_path / "nginx.conf")
    assert env["LUMIVERB_ROOT_MAP"] == '{"/": "/"}' and env["LUMIVERB_RESET_ON_THIS_MACHINE"] == "true"
    assert (tmp_path / "nginx.conf").exists()
    capsys.readouterr()
    assert cli.main(["--env", str(env_file), "init"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"· {env_file} kept", f"· {tmp_path / 'nginx.conf'} kept"]


def test_the_command_wants_a_program_and_its_config_together(tmp_path, capsys):
    assert cli.main(["init", "--env", str(tmp_path / "env"), "--data-dir", str(tmp_path / "var"),
                     "--nginx-bin", str(OPT / "nginx/bin/nginx")]) == 1
    assert capsys.readouterr().err.strip().startswith("✗ Name nginx with its config")
    assert not (tmp_path / "env").exists()


def test_the_command_needs_the_env_file_named(capsys):
    assert cli.main(["init", "--data-dir", "/nowhere"]) == 1
    assert capsys.readouterr().err.strip() == "✗ Say which env file: --env FILE or $LUMIVERB_ENV_FILE"


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_folder_it_cant_write_is_one_line(tmp_path, capsys):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert cli.main(["init", "--env", str(locked / "env"), "--data-dir", str(tmp_path / "var")]) == 1
    finally:
        locked.chmod(0o700)
    assert capsys.readouterr().err.strip() == f"✗ Can't write {locked / 'env'}: Permission denied"


def test_the_root_map_it_writes_is_the_one_the_scheduler_reads(tmp_path, monkeypatch):
    from src.server.scheduler.settings import SchedulerSettings

    env = read_env_file(_install(tmp_path)["env"])
    monkeypatch.setenv("LUMIVERB_ROOT_MAP", env["LUMIVERB_ROOT_MAP"])
    assert SchedulerSettings().root_map == {"/": "/"}


def test_the_reset_setting_it_writes_is_the_one_the_api_reads(tmp_path, monkeypatch):
    from src.server.config import Settings

    env = read_env_file(_install(tmp_path)["env"])
    monkeypatch.setenv("LUMIVERB_RESET_ON_THIS_MACHINE", env["LUMIVERB_RESET_ON_THIS_MACHINE"])
    assert Settings(control_plane_database_url="x", tenant_database_url_template="x").lumiverb_reset_on_this_machine
    monkeypatch.delenv("LUMIVERB_RESET_ON_THIS_MACHINE")
    assert not Settings(control_plane_database_url="x", tenant_database_url_template="x",
                        _env_file=None).lumiverb_reset_on_this_machine, "off unless the install says so"
