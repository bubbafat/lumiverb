"""Lumiverb's own plug-ins (src/server/own_plugins.py, ADR-023 phase 3), as
migrating installs them, against a real control database: GeoNames,
shipped in src/own_plugins/geonames/, installed off by default and recorded
as by lumiverb; the same version again changes nothing; a newer one keeps
the server admin's default and what they accepted (the accounts following
it waiting when that no longer covers the new text); one the server admin
uninstalled stays uninstalled until they install it again; an install's
LUMIVERB_GEONAMES_URL becomes its mirror once, never over one the server
admin chose; one that can't be installed (no DATA_DIR, a manifest this
Lumiverb doesn't read) is said, kept with why once and shown on the Admin
page, the server going on; two installs at once install it once; and
`lumiverb-server migrate` and `lumiverb-server plugins` do it and say it."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
from sqlalchemy import text

from src.server import own_plugins
from src.server.database import get_control_session
from src.server.lifecycle import cli
from src.server.own_plugins import BY, NO_DATA_DIR, install_own, status
from src.server.plugin_install import program_dir
from src.server.plugin_settings import SERVER, PluginSettingsRepository
from src.server.repository.installed_plugins import InstalledPluginRepository
from src.server.repository.plugins import OFF, ON, WAITING, state_of
from src.server.system_health import GREEN, RED, YELLOW, own_plugins_row
from src.shared.utils import utcnow
from tests.test_first_run_setup import _env_file, control  # noqa: F401 (control: a fixture)

pytestmark = pytest.mark.slow
GEONAMES = "geonames"


def _row() -> dict | None:
    with get_control_session() as session:
        return InstalledPluginRepository(session).get(GEONAMES)


def _log() -> list[dict]:
    with get_control_session() as session:
        return InstalledPluginRepository(session).log(GEONAMES)


def _status() -> list[dict]:
    with get_control_session() as session:
        return status(session)


def _install(url: str, data: Path | None, **kw) -> list[str]:
    said: list[str] = []
    install_own(url, data, say=said.append, **kw)
    return said


def _shipped(tmp_path: Path, monkeypatch, **change) -> Path:
    """Lumiverb shipping GeoNames otherwise: its manifest changed (a later release's, say)."""
    folder = tmp_path / "own" / GEONAMES
    shutil.copytree(own_plugins.OWN / GEONAMES, folder)
    manifest = json.loads((folder / "lumiverb-plugin.json").read_text())
    for key, value in change.items():
        if isinstance(value, dict) and isinstance(manifest.get(key), dict):
            manifest[key] = {**manifest[key], **value}
        else:
            manifest[key] = value
    (folder / "lumiverb-plugin.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(own_plugins, "OWN", folder.parent)
    own_plugins.shipped.cache_clear()
    return folder


@pytest.fixture(autouse=True)
def _shipped_cache():
    own_plugins.shipped.cache_clear()
    yield
    own_plugins.shipped.cache_clear()


def test_it_comes_installed_off_by_default_once(control, tmp_path) -> None:  # noqa: F811
    data = tmp_path / "data"
    said = _install(control, data)
    row = _row()
    assert (row["version"], row["on_by_default"], row["acknowledged_version"], row["installed_by"]) == (
        "1", False, None, BY)
    assert said == ["✓ Place names (GeoNames) 1 installed, off until an account's admin turns it on"]
    assert (program_dir(data, GEONAMES) / "geonames.py").read_text() == (own_plugins.OWN / GEONAMES / "geonames.py").read_text()
    # Again (every start migrates): nothing changes, nothing more is kept.
    assert _install(control, data) == [] and len(_log()) == 1
    assert _status() == [{"plugin_id": GEONAMES, "title": "Place names (GeoNames)", "shipped": "1", "installed": "1",
                          "failed": None, "uninstalled": False}]
    assert own_plugins_row(own=_status(), now=utcnow()).state == GREEN


def test_a_newer_one_keeps_the_server_admins_default_and_what_they_accepted(control, tmp_path, monkeypatch) -> None:  # noqa: F811
    data = tmp_path / "data"
    _install(control, data)
    with get_control_session() as session:
        InstalledPluginRepository(session).set_default(GEONAMES, True, acknowledged_version="3", by="robert",
                                                      now=utcnow())
        session.commit()
    # A later release rewords its text: what was accepted stands.
    _shipped(tmp_path / "a", monkeypatch, version="2", acknowledgment={"version": "3.1"})
    assert _install(control, data) == ["✓ Place names (GeoNames) updated from 1 to 2"]
    row = _row()
    assert (row["version"], row["on_by_default"], row["acknowledged_version"], row["changed_by"]) == ("2", True, "3", BY)
    assert _log()[-1]["by"] == BY and _log()[-1]["action"] == "install"
    from src.plugins.manifest import parse

    def follower() -> str:
        r = _row()
        return state_of(parse(r["manifest"]).spec(), None, {"enabled": r["on_by_default"],
                                                             "acknowledged_version": r["acknowledged_version"]})

    assert follower() == ON
    # A later one asks it again: nobody accepts it for them, so the accounts following the default wait.
    _shipped(tmp_path / "b", monkeypatch, version="3", acknowledgment={"version": "4", "reaccept_from": "4"})
    _install(control, data)
    assert _row()["acknowledged_version"] == "3" and follower() == WAITING
    # One that reads more is accepted with Lumiverb, and said.
    _shipped(tmp_path / "c", monkeypatch, version="4", internet=True)
    assert _install(control, data) == ["✓ Place names (GeoNames) updated from 3 to 4 (it also: Uses the internet, "
                                       "accepted with Lumiverb)"]


def test_one_the_server_admin_uninstalled_stays_uninstalled_until_they_install_it(control, tmp_path, capsys,  # noqa: F811
                                                                                 monkeypatch) -> None:
    data = tmp_path / "data"
    env = _env_file(tmp_path, control)
    env.write_text(env.read_text() + f"DATA_DIR={data}\n")
    # The command puts the file's settings in the environment, where they'd outlast the test (the environment's
    # come before the file's): this test's are put there first, to be put back after.
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("APP_HOST", "http://localhost:6464")
    assert cli.main(["migrate", "--env", str(env)]) == 0
    assert "✓ Place names (GeoNames) 1 installed" in capsys.readouterr().out
    assert cli.main(["plugins", "uninstall", GEONAMES, "--yes", "--env", str(env)]) == 0
    said = _install(control, data)
    assert _row() is None and said == [
        f"Place names (GeoNames): uninstalled by the server admin, so not installed again "
        f"(`lumiverb-server plugins install {own_plugins.OWN / GEONAMES}` puts it back)"]
    assert _status()[0]["uninstalled"] and own_plugins_row(own=_status(), now=utcnow()).state == GREEN
    assert cli.main(["plugins", "--env", str(env)]) == 0
    assert "Lumiverb's own: geonames (Place names (GeoNames)) uninstalled by the server admin" in capsys.readouterr().out
    assert cli.main(["plugins", "install", str(own_plugins.OWN / GEONAMES), "--off", "--yes", "--env", str(env)]) == 0
    assert _row()["version"] == "1"
    assert _install(control, data) == [], "theirs again: nothing to do"


def test_the_install_s_geonames_mirror_becomes_its_mirror_once(control, tmp_path) -> None:  # noqa: F811
    def mirror() -> str | None:
        with get_control_session() as session:
            return PluginSettingsRepository(session, SERVER).get(GEONAMES).get("mirror")

    data = tmp_path / "data"
    said = _install(control, data, geonames_url="http://mirror.lan/geonames/")
    assert mirror() == "http://mirror.lan/geonames/" and said[-1] == "  its mirror: http://mirror.lan/geonames/ (LUMIVERB_GEONAMES_URL)"
    with get_control_session() as session:
        PluginSettingsRepository(session, SERVER).save(GEONAMES, {"mirror": "https://chosen.example/dump/"}, by="robert",
                                                      now=utcnow())
        session.commit()
    _install(control, data, geonames_url="http://other.lan/")
    assert mirror() == "https://chosen.example/dump/", "the server admin's, kept"
    # Installed again after an uninstall: not its first install, so nothing carried over again.
    with get_control_session() as session:
        InstalledPluginRepository(session).remove(GEONAMES, by="lumiverb-test", now=utcnow())
        session.execute(text("DELETE FROM plugin_server_settings"))
        session.execute(text("DELETE FROM plugin_install_log WHERE action = 'uninstall'"))
        session.commit()
    _install(control, data, geonames_url="http://again.lan/")
    assert mirror() is None


@pytest.mark.parametrize("url", ["https://download.geonames.org/export/dump/", "https://download.geonames.org/export/dump",
                                 "", "ftp://nowhere/"])
def test_geonames_own_server_or_no_address_is_no_mirror(control, tmp_path, url) -> None:  # noqa: F811
    _install(control, tmp_path / "data", geonames_url=url)
    with get_control_session() as session:
        assert PluginSettingsRepository(session, SERVER).get(GEONAMES) == {}


def test_one_that_cant_be_installed_is_said_kept_with_why_once_and_shown(control, tmp_path, monkeypatch) -> None:  # noqa: F811
    failed: list[str] = []
    install_own(control, None, say=lambda line: None, fail=failed.append)
    assert failed == [f"✗ geonames couldn't be installed: {NO_DATA_DIR}"] and _row() is None
    install_own(control, None, say=lambda line: None, fail=failed.append)
    assert [(e["action"], e["why"], e["by"]) for e in _log()] == [("failed", NO_DATA_DIR, BY)], "kept once"
    row = own_plugins_row(own=_status(), now=utcnow())
    assert row.state == RED and row.reason == (f"Place names (GeoNames) couldn't be installed: {NO_DATA_DIR}. It's "
                                               "tried again at the next start.")
    # A manifest this Lumiverb doesn't read: said, kept, and the others go on.
    _shipped(tmp_path, monkeypatch, protocol=99)
    install_own(control, tmp_path / "data", say=lambda line: None, fail=failed.append)
    assert "Its manifest isn't one this Lumiverb reads" in failed[-1] and _row() is None
    # Fixed (the next release, the next start): installed, and the row is green again.
    monkeypatch.setattr(own_plugins, "OWN", Path(own_plugins.__file__).resolve().parents[1] / "own_plugins")
    own_plugins.shipped.cache_clear()
    install_own(control, tmp_path / "data", say=lambda line: None)
    assert _row()["version"] == "1" and own_plugins_row(own=_status(), now=utcnow()).state == GREEN


def test_an_older_one_waiting_for_the_next_start_is_yellow(control, tmp_path, monkeypatch) -> None:  # noqa: F811
    _install(control, tmp_path / "data")
    _shipped(tmp_path, monkeypatch, version="2")
    row = own_plugins_row(own=_status(), now=utcnow())
    assert row.state == YELLOW and row.reason == "Place names (GeoNames): installed at the next start of Lumiverb."


def test_two_installs_at_once_install_it_once(control, tmp_path) -> None:  # noqa: F811
    data = tmp_path / "data"
    both = [threading.Thread(target=_install, args=(control, data)) for _ in range(2)]
    for t in both:
        t.start()
    for t in both:
        t.join(120)
    assert [e["action"] for e in _log()] == ["install"] and _row()["version"] == "1"


def test_every_account_that_had_it_on_has_it_on_and_no_one_else(control, tmp_path) -> None:  # noqa: F811
    """GeoNames' acknowledgment as installed is the built-in's, version 3 accepted again only from 1: an account
    that accepted 1, 2 or 3 has it on without being asked; one that turned it off, or never chose, has it off."""
    from src.server.plugin_catalog import find

    _install(control, tmp_path / "data")
    entry = find(GEONAMES)
    for accepted in ("1", "2", "3"):
        assert state_of(entry.spec, {"enabled": True, "acknowledged_version": accepted}, entry.installed.default) == ON
    assert state_of(entry.spec, {"enabled": False, "acknowledged_version": None}, entry.installed.default) == OFF
    assert state_of(entry.spec, None, entry.installed.default) == OFF


@pytest.mark.parametrize("with_data_dir", [True, False], ids=["data-dir", "no-data-dir"])
def test_a_linux_update_installs_it_as_migrate_sh_s_last_step(control, tmp_path, with_data_dir) -> None:  # noqa: F811
    """scripts/migrate.sh, as update-api.sh runs it (the service user's environment: the control database, DATA_DIR
    and LUMIVERB_GEONAMES_URL), with no account yet: its last step installs GeoNames, making DATA_DIR/plugins;
    without DATA_DIR it says why, and the update goes on."""
    import os
    import subprocess

    data = tmp_path / "data"
    env = {**os.environ, "CONTROL_PLANE_DATABASE_URL": control, "UV_BIN": shutil.which("uv"),
           "DATA_DIR": str(data) if with_data_dir else "", "LUMIVERB_GEONAMES_URL": "http://mirror.lan/"}
    out = subprocess.run(["bash", "scripts/migrate.sh"], cwd=own_plugins.OWN.parents[1], env=env, capture_output=True,
                         text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "=== Step 4: Lumiverb's own plug-ins ===" in out.stdout
    if with_data_dir:
        assert "✓ Place names (GeoNames) 1 installed" in out.stdout and _row()["installed_by"] == BY
        assert (program_dir(data, GEONAMES) / "geonames.py").is_file()
    else:
        assert f"✗ geonames couldn't be installed: {NO_DATA_DIR}" in out.stderr and _row() is None
        assert _log()[-1]["why"] == NO_DATA_DIR


def test_the_admin_page_says_it_to_a_server_admin(control) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from src.server.api.main import app
    from tests.test_server_admins import HERE, LOCALHOST, admin_with_database, drop_databases

    install_own(control, None, say=lambda line: None)
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        robert, _tenant = admin_with_database(client, control)
        try:
            rows = {r["key"]: r for r in client.get("/v1/system/health", headers=robert).json()["rows"]}
            assert rows["own_plugins"]["state"] == "red" and NO_DATA_DIR in rows["own_plugins"]["reason"]
            assert rows["own_plugins"]["link"] == "/settings/plugins"
        finally:
            drop_databases(control)
