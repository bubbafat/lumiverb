"""A Mac install made before 6465 was HTTPS's had its API there: run's first
start moves it to 6364, once (src/server/lifecycle/api_port.py), the env file
and nginx's config each backed up first; one changed by hand, or 6364 in use,
is left as it is. The ports devices use: test_server_network.py."""

from __future__ import annotations

import datetime as dt
import stat

import pytest

from src.server.lifecycle import api_port, network
from src.server.lifecycle.envfile import read_env_file
from src.server.lifecycle.init import nginx_conf, nginx_site

pytestmark = pytest.mark.fast

NOW = dt.datetime(2026, 10, 17, 15, 0, tzinfo=dt.UTC)


def _install(tmp_path, *, api="6465", tls: str | None = "6443", upstream_port="6465", pg_bin=True):
    config = tmp_path / "nginx.conf"
    config.write_text(nginx_conf(nginx_site(listen=(network.include_line(tmp_path),), server_name="_",
                                            root=tmp_path / "web", upstream=f"http://127.0.0.1:{upstream_port}")))
    config.chmod(0o644)
    lines = ["# Written by lumiverb-server init", "CONTROL_PLANE_DATABASE_URL=postgresql+psycopg2://u:p@127.0.0.1:6468/cp",
             "API_LISTEN_HOST=127.0.0.1", f"API_PORT={api}", "WEB_PORT=6464",
             *([f"WEB_TLS_PORT={tls}"] if tls else []), *(["PG_BIN=/opt/homebrew/opt/postgresql@18/bin"] if pg_bin else []),
             f"NGINX_CONFIG={config}", "# API_PORT=6465 in a comment stays"]
    env_file = tmp_path / "env"
    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)
    return env_file, config


def test_an_old_mac_install_s_api_moves_to_6364_and_https_to_6465(tmp_path):
    env_file, config = _install(tmp_path)
    env_before, config_before = env_file.read_text(), config.read_text()
    env, said = api_port.moved(env_file, read_env_file(env_file), now=NOW, free=lambda port: True)
    assert (env["API_PORT"], env["WEB_TLS_PORT"]) == ("6364", "6465")
    assert read_env_file(env_file) == {**read_env_file(tmp_path / "env.before-ports-20261017T150000Z"),
                                       "API_PORT": "6364", "WEB_TLS_PORT": "6465"}
    assert env_file.read_text() == env_before.replace("API_PORT=6465\n", "API_PORT=6364\n", 1).replace(
        "WEB_TLS_PORT=6443", "WEB_TLS_PORT=6465"), "the rest as it was, the comment too"
    assert config.read_text() == config_before.replace("proxy_pass http://127.0.0.1:6465;",
                                                       "proxy_pass http://127.0.0.1:6364;")
    assert config.read_text().count("proxy_pass http://127.0.0.1:6364;") == 2
    assert (tmp_path / "env.before-ports-20261017T150000Z").read_text() == env_before
    assert (tmp_path / "nginx.conf.before-ports-20261017T150000Z").read_text() == config_before
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600 and stat.S_IMODE(config.stat().st_mode) == 0o644
    assert said.startswith("The API moved from port 6465 to 6364, HTTPS's default from 6443 to 6465")
    assert not list(tmp_path.glob("*.part"))

    again, said = api_port.moved(env_file, read_env_file(env_file), now=NOW, free=lambda port: True)
    assert said is None and again["API_PORT"] == "6364", "once"


@pytest.mark.parametrize(("tls", "kept"), [("8443", "8443"), (None, None)])
def test_an_admin_s_https_port_or_none_is_left_alone(tmp_path, tls, kept):
    env_file, _ = _install(tmp_path, tls=tls)
    env, _ = api_port.moved(env_file, read_env_file(env_file), now=NOW, free=lambda port: True)
    assert env["API_PORT"] == "6364" and env.get("WEB_TLS_PORT") == kept
    assert read_env_file(env_file).get("WEB_TLS_PORT") == kept


def test_a_config_changed_by_hand_or_6364_in_use_leaves_both_alone(tmp_path):
    env_file, config = _install(tmp_path, upstream_port="7000")
    before = env_file.read_text(), config.read_text()
    env, said = api_port.moved(env_file, read_env_file(env_file), now=NOW, free=lambda port: True)
    assert env["API_PORT"] == "6465" and "doesn't pass /v1/ there as init wrote it" in said
    assert (env_file.read_text(), config.read_text()) == before and not list(tmp_path.glob("*.before-ports-*"))

    env_file, config = _install(tmp_path)
    env, said = api_port.moved(env_file, read_env_file(env_file), now=NOW, free=lambda port: port != 6364)
    assert env["API_PORT"] == "6465" and said == "The API stays on 6465: something else on this Mac is using 6364"
    assert not list(tmp_path.glob("*.before-ports-*"))


@pytest.mark.parametrize("install", [{"api": "6364", "upstream_port": "6364"}, {"pg_bin": False}])
def test_nothing_is_done_for_a_new_install_or_a_linux_one(tmp_path, install):
    env_file, config = _install(tmp_path, **install)
    before = env_file.read_text()
    env, said = api_port.moved(env_file, read_env_file(env_file), now=NOW, free=lambda port: True)
    assert said is None and env_file.read_text() == before
