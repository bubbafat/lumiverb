"""A Mac install moves to an ordinary database role at run's first start
(src/server/lifecycle/roles.py): its env file's database URLs become the
role's, the superuser's kept for run alone, the file backed up first and
nothing else in it changed; the API and the scheduler never get the
superuser's URL; and the databases are handed over before anything else
touches them. The handover itself, on real PostgreSQL: test_db_role_slow.py."""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import make_url

from src.server.lifecycle import cli, roles
from src.server.lifecycle.envfile import read_env_file

pytestmark = pytest.mark.fast

SUPER = "postgresql+psycopg2://lumiverb:old-pw@127.0.0.1:6432"
OLD = f"""# Written by lumiverb-server init. Yours to edit.

# Database
CONTROL_PLANE_DATABASE_URL={SUPER}/control_plane
TENANT_DATABASE_URL_TEMPLATE={SUPER}/{{tenant_id}}

# Auth
ADMIN_KEY=k
# A line of Robert's own
LUMIVERB_ROOT_MAP='{{"/": "/"}}'
PG_BIN=/opt/homebrew/opt/postgresql@18/bin
"""
NOW = datetime(2026, 10, 17, 1, 2, 3, tzinfo=UTC)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / "env"
    path.write_text(OLD)
    path.chmod(0o600)
    return path


def test_the_env_file_moves_to_the_role_and_keeps_the_superuser_for_run(env_file):
    env = roles.with_app_role(env_file, read_env_file(env_file), password="new-pw", now=NOW)
    on_disk = read_env_file(env_file)
    assert on_disk == env
    for name in roles.URLS:
        url = make_url(on_disk[name].replace("{tenant_id}", "x"))
        assert (url.username, url.password, url.host, url.port) == (roles.APP_ROLE, "new-pw", "127.0.0.1", 6432)
    assert on_disk["TENANT_DATABASE_URL_TEMPLATE"].endswith("/{tenant_id}")
    assert on_disk[roles.SUPERUSER_URL] == f"{SUPER}/control_plane"
    assert (on_disk["ADMIN_KEY"], on_disk["LUMIVERB_ROOT_MAP"]) == ("k", '{"/": "/"}')


def test_nothing_else_in_the_file_changes_and_the_old_one_is_kept_beside_it(env_file):
    roles.with_app_role(env_file, read_env_file(env_file), password="new-pw", now=NOW)
    backup = env_file.with_name("env.before-app-role-20261017T010203Z")
    assert backup.read_text() == OLD
    new_lines = env_file.read_text().splitlines()
    assert [line for line in new_lines if not line.startswith(("CONTROL_PLANE", "TENANT_DATABASE", roles.SUPERUSER_URL))
            and not line.startswith("# The superuser") and not line.startswith("# it what it owns")] \
        == [line for line in OLD.splitlines() if not line.startswith(("CONTROL_PLANE", "TENANT_DATABASE"))]
    assert new_lines.index(f"{roles.SUPERUSER_URL}={SUPER}/control_plane") \
        == new_lines.index(next(line for line in new_lines if line.startswith("TENANT_DATABASE"))) + 3
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600, "it holds the keys: still its owner's alone"


def test_a_file_already_moved_over_is_left_alone(env_file):
    first = roles.with_app_role(env_file, read_env_file(env_file), password="new-pw", now=NOW)
    text = env_file.read_text()
    assert roles.with_app_role(env_file, first) == first
    assert env_file.read_text() == text
    assert len(list(env_file.parent.glob("env.before-app-role-*"))) == 1


def test_only_an_install_with_its_own_postgres_moves_over(env_file, tmp_path):
    linux = tmp_path / "linux-env"
    linux.write_text(OLD.replace("PG_BIN=/opt/homebrew/opt/postgresql@18/bin\n", ""))
    env = read_env_file(linux)
    assert cli.as_app_role(env, linux) == env, "a Linux install's role is already an ordinary one"
    assert linux.read_text() == OLD.replace("PG_BIN=/opt/homebrew/opt/postgresql@18/bin\n", "")
    moved = cli.as_app_role(read_env_file(env_file), env_file)
    assert make_url(moved["CONTROL_PLANE_DATABASE_URL"]).username == roles.APP_ROLE


def test_neither_the_api_nor_the_scheduler_gets_the_superuser(env_file, tmp_path):
    env = {**roles.with_app_role(env_file, read_env_file(env_file), password="new-pw", now=NOW),
           "DATA_DIR": str(tmp_path)}
    env.pop("PG_BIN")
    api, scheduler = cli.server_children(env, python="/py", cwd=tmp_path)
    for child in (api, scheduler):
        assert roles.SUPERUSER_URL not in child.env
        assert all("old-pw" not in value for value in child.env.values()), child.name
    assert make_url(api.env["CONTROL_PLANE_DATABASE_URL"]).username == roles.APP_ROLE


def test_the_databases_are_handed_over_before_anything_touches_them(env_file):
    env = roles.with_app_role(env_file, read_env_file(env_file), password="new-pw", now=NOW)
    order = []
    with (patch.object(roles, "ensure_app_role", side_effect=lambda su, url: order.append(("hand over", su, url))),
          patch.object(cli, "create_database_if_missing", side_effect=lambda url: order.append(("create", url))),
          patch.object(cli, "backed_up_before_upgrade", side_effect=lambda e, url: order.append(("back up", url))),
          patch.object(cli, "migrate", side_effect=lambda url, say: order.append(("migrate", url)) or 0)):
        cli._created_and_migrated(env["CONTROL_PLANE_DATABASE_URL"], env)
    app = env["CONTROL_PLANE_DATABASE_URL"]
    assert order == [("hand over", f"{SUPER}/control_plane", app), ("create", app), ("back up", app), ("migrate", app)]


def test_an_install_still_as_the_superuser_isnt_handed_over_by_migrate_alone(env_file):
    # lumiverb-server migrate, backup, restore: as the env file says, never moving it over.
    env = read_env_file(env_file)
    with (patch.object(roles, "ensure_app_role") as hand_over,
          patch.object(cli, "create_database_if_missing", return_value=False),
          patch.object(cli, "backed_up_before_upgrade"),
          patch.object(cli, "migrate", return_value=0)):
        cli._created_and_migrated(env["CONTROL_PLANE_DATABASE_URL"], env)
    hand_over.assert_not_called()


def test_initdb_makes_the_superuser_not_the_role(env_file, tmp_path):
    from src.server.lifecycle import postgres

    env = roles.with_app_role(env_file, read_env_file(env_file), password="new-pw", now=NOW)
    seen = {}

    def initdb(argv, **kwargs):
        seen["user"] = argv[argv.index("-U") + 1]
        pwfile = next(a for a in argv if a.startswith("--pwfile="))[len("--pwfile="):]
        seen["password"] = Path(pwfile).read_text()
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(postgres.subprocess, "run", side_effect=initdb):
        postgres.make_cluster_if_missing(tmp_path / "bin", tmp_path, env.get(roles.SUPERUSER_URL) or env["CONTROL_PLANE_DATABASE_URL"])
    assert seen == {"user": "lumiverb", "password": "old-pw"}


@pytest.mark.parametrize("url", ["postgresql://x", "postgresql://127.0.0.1:6468/control_plane", "not a url"])
def test_a_url_naming_no_superuser_is_left_alone(tmp_path, url):
    """Nothing to move over: run goes on, and initdb says what's missing."""
    env_file = tmp_path / "env"
    env_file.write_text(f"CONTROL_PLANE_DATABASE_URL={url}\nPG_BIN=/pg\n")
    env = read_env_file(env_file)
    assert roles.with_app_role(env_file, env) == env
    assert env_file.read_text() == f"CONTROL_PLANE_DATABASE_URL={url}\nPG_BIN=/pg\n"
    assert not list(tmp_path.glob("env.before-app-role-*"))
