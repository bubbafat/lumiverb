"""Installing a plug-in (ADR-023 phase 1a), against a real install and its
account: `lumiverb-server plugins` on the machine shows what a plug-in
reads and reaches and asks before installing it, copies its folder in
read-only, and records it on or off by default; every account follows the
default until its admin chooses, and the admin's choice then stands; on by
default, its acknowledgment is the server admin's to accept for those
accounts; an update asking more asks again, one asking the same doesn't;
uninstalling takes it away. Everything kept in the install's log."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from src.plugins.contract import Acknowledgment, PluginSpec
from src.server.config import get_settings
from src.server.database import get_control_session
from src.server.lifecycle import cli
from src.server.plugin_install import install_mark, program_dir
from src.server.repository.installed_plugins import InstalledPluginRepository
from src.server.repository.plugins import state_of
from tests.test_first_run_setup import _env_file, control  # noqa: F401 (control: a fixture)
from tests.test_plugin_runner import _until
from tests.test_plugin_runner import data as short_data  # noqa: F401 (a fixture)
from tests.test_server_admins import install  # noqa: F401 (a fixture)

HELLO = Path(__file__).parent / "plugins" / "hello"
ACK = {"version": "1", "title": "Hello's terms", "text": "Say hello nicely.\n\nThat's all.",
       "links": [{"label": "The terms", "url": "https://example.org/terms"}]}


@pytest.fixture
def machine(install, control, tmp_path, monkeypatch):  # noqa: F811
    """The install's env file, its data folder in tmp_path: (env file, data folder). The command puts the
    file's settings in the environment, where they'd outlast the test (the environment's come before the
    file's): this test's data folder is put there first, to be put back after."""
    env = _env_file(tmp_path, control)
    data = tmp_path / "data"
    env.write_text(env.read_text() + f"DATA_DIR={data}\n")
    monkeypatch.setenv("DATA_DIR", str(data))
    return str(env), data


def _hello(tmp_path: Path, name: str = "hello", **change) -> Path:
    """A copy of the test plug-in, its manifest changed."""
    folder = tmp_path / "src" / name
    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(HELLO, folder)
    manifest = json.loads((folder / "lumiverb-plugin.json").read_text())
    manifest.update(change)
    (folder / "lumiverb-plugin.json").write_text(json.dumps(manifest))
    return folder


def _answers(monkeypatch, *said: str) -> list[str]:
    """Each question asked, answered in turn."""
    asked: list[str] = []
    replies = iter(said)

    def answer(question: str) -> str:
        asked.append(question)
        return next(replies)

    monkeypatch.setattr("builtins.input", answer)
    return asked


def _listed(client, headers) -> dict:
    r = client.get("/v1/plugins", headers=headers)
    assert r.status_code == 200, r.text
    return {p["plugin_id"]: p for p in r.json()["plugins"]}


@pytest.mark.fast
def test_an_account_that_hasnt_chosen_has_the_default() -> None:
    spec = PluginSpec("p", "P", "")
    on, off = {"enabled": True, "acknowledged_version": None}, {"enabled": False, "acknowledged_version": None}
    assert state_of(spec, None, on) == "on" and state_of(spec, None, off) == "off"
    assert state_of(spec, off, on) == "off", "its admin's choice stands"
    assert state_of(spec, None) == "off", "Lumiverb's own have no default"
    asks = PluginSpec("p", "P", "", acknowledgment=Acknowledgment("2", "T", "x", reaccept_from="2"))
    assert state_of(asks, None, {"enabled": True, "acknowledged_version": "1"}) == "waiting"


@pytest.mark.slow
def test_installing_shows_what_it_does_asks_and_copies_it_in_read_only(install, machine, tmp_path, capsys,  # noqa: F811
                                                                       monkeypatch):
    client, robert, _ = install
    env, data = machine
    folder = _hello(tmp_path, work={"title": "Hellos", "version": "1", "media": ["image"],
                                    "inputs": ["location", "preview"],
                                    "outputs": [{"key": "said", "kind": "fact", "label": "Said"}]})
    asked = _answers(monkeypatch, "n")
    assert cli.main(["plugins", "install", str(folder), "--off", "--env", env]) == 1
    out = capsys.readouterr().out
    assert "Hello 1, by Lumiverb's tests (hello)" in out
    assert "  • Reads each clip's location (a person's, else its GPS, else an applied guess)" in out
    assert "  • Reads each photo's preview image" in out and "  • Doesn't use the internet" in out
    assert asked == ["Install it, off until an account's admin turns it on? [y/N] "]
    assert "hello" not in _listed(client, robert) and not program_dir(data, "hello").exists()

    _answers(monkeypatch, "y")
    assert cli.main(["plugins", "install", str(folder), "--off", "--env", env]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "✓ Installed hello 1: off until an account's admin turns it on"
    copy = program_dir(data, "hello")
    assert (copy / "hello.py").read_text() == (HELLO / "hello.py").read_text()
    for root, _dirs, names in os.walk(copy):
        for path in [root, *(os.path.join(root, n) for n in names if not os.path.islink(os.path.join(root, n)))]:
            assert not os.lstat(path).st_mode & 0o222, f"{path} can be written"

    listed = _listed(client, robert)["hello"]
    assert (listed["kind"], listed["version"], listed["author"], listed["state"]) == (
        "installed", "1", "Lumiverb's tests", "off")
    assert (listed["on_by_default"], listed["follows_default"]) == (False, True)
    assert listed["reads"] == ["each clip's location (a person's, else its GPS, else an applied guess)",
                               "each photo's preview image"]
    assert listed["internet"] is False and listed["downloads"] == [] and "data" not in listed

    assert cli.main(["plugins", "--env", env]) == 0
    assert "  hello 1  Hello: off by default" in capsys.readouterr().out


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() == 0, reason="root writes any file")
def test_its_copy_cant_be_changed_or_added_to(machine, tmp_path):
    env, data = machine
    assert cli.main(["plugins", "install", str(_hello(tmp_path)), "--off", "--yes", "--env", env]) == 0
    copy = program_dir(data, "hello")
    with pytest.raises(PermissionError):
        (copy / "hello.py").write_text("changed")
    with pytest.raises(PermissionError):
        (copy / "new.py").write_text("added")
    assert (copy / "hello.py").read_text() == (HELLO / "hello.py").read_text()


@pytest.mark.slow
def test_every_account_follows_the_default_until_its_admin_chooses(install, machine, tmp_path, monkeypatch):  # noqa: F811
    client, robert, _ = install
    env, _ = machine
    assert cli.main(["plugins", "install", str(_hello(tmp_path)), "--off", "--yes", "--env", env]) == 0
    assert _listed(client, robert)["hello"]["state"] == "off"
    assert cli.main(["plugins", "default", "hello", "on", "--env", env]) == 0
    listed = _listed(client, robert)["hello"]
    assert (listed["state"], listed["on_by_default"], listed["follows_default"]) == ("on", True, True)

    # Its admin turns it off: theirs stands, whatever the default.
    r = client.put("/v1/plugins/hello", json={"enabled": False}, headers=robert)
    assert r.status_code == 200 and r.json()["state"] == "off" and r.json()["follows_default"] is False, r.text
    assert cli.main(["plugins", "default", "hello", "off", "--env", env]) == 0
    assert cli.main(["plugins", "default", "hello", "on", "--env", env]) == 0
    assert _listed(client, robert)["hello"]["state"] == "off"
    r = client.put("/v1/plugins/hello", json={"enabled": True}, headers=robert)
    assert r.status_code == 200 and r.json()["state"] == "on", r.text
    assert cli.main(["plugins", "default", "hello", "off", "--env", env]) == 0
    assert _listed(client, robert)["hello"]["state"] == "on"


@pytest.mark.slow
def test_on_by_default_its_acknowledgment_is_the_server_admins_to_accept(install, machine, tmp_path, capsys,  # noqa: F811
                                                                         monkeypatch):
    client, robert, _ = install
    env, _ = machine
    folder = _hello(tmp_path, acknowledgment=ACK)
    asked = _answers(monkeypatch, "y", "n")
    assert cli.main(["plugins", "install", str(folder), "--on", "--env", env]) == 1
    out = capsys.readouterr().out
    assert "Hello's terms (version 1)" in out and "  Say hello nicely." in out and "https://example.org/terms" in out
    assert asked[-1].startswith("\nOn by default, you accept this for every account that hasn't chosen.")
    assert "hello" not in _listed(client, robert), "nothing installed"

    _answers(monkeypatch, "y", "y")
    assert cli.main(["plugins", "install", str(folder), "--on", "--env", env]) == 0
    listed = _listed(client, robert)["hello"]
    assert listed["state"] == "on" and listed["acknowledged"]["version"] == "1"
    assert listed["acknowledged"]["by"] is None, "the server admin's, for every account that hasn't chosen"

    # A newer text under the same terms asks nothing more; one saying accept again does.
    asked = _answers(monkeypatch)
    assert cli.main(["plugins", "install", str(_hello(tmp_path, version="2", acknowledgment={**ACK, "version": "2",
                                                                                            "reaccept_from": "1"})),
                     "--env", env]) == 0
    assert asked == [] and _listed(client, robert)["hello"]["acknowledged"]["version"] == "1"
    asked = _answers(monkeypatch, "y")
    assert cli.main(["plugins", "install", str(_hello(tmp_path, version="3", acknowledgment={**ACK, "version": "3",
                                                                                            "reaccept_from": "3"})),
                     "--env", env]) == 0
    assert len(asked) == 1 and _listed(client, robert)["hello"]["acknowledged"]["version"] == "3"

    # An account admin turning off then on accepts it themselves, as ever (ADR-021).
    assert client.put("/v1/plugins/hello", json={"enabled": False}, headers=robert).status_code == 200
    r = client.put("/v1/plugins/hello", json={"enabled": True}, headers=robert)
    assert r.status_code == 409 and r.json()["error"]["code"] == "acknowledgment_required", r.text
    r = client.put("/v1/plugins/hello", json={"enabled": True, "acknowledged": "3"}, headers=robert)
    assert r.status_code == 200 and r.json()["acknowledged"]["by"] == "robert@example.com", r.text


@pytest.mark.slow
def test_an_update_that_reads_more_asks_again_one_that_doesnt_doesnt(install, machine, tmp_path, capsys,  # noqa: F811
                                                                    monkeypatch):
    client, robert, _ = install
    env, data = machine
    assert cli.main(["plugins", "install", str(_hello(tmp_path)), "--on", "--yes", "--env", env]) == 0
    first = install_mark(program_dir(data, "hello"))
    asked = _answers(monkeypatch)
    assert cli.main(["plugins", "install", str(_hello(tmp_path, version="2")), "--env", env]) == 0
    assert install_mark(program_dir(data, "hello")) not in (None, first), "each install its own: the runner restarts it"
    assert asked == [] and "This version reads and reaches nothing more." in capsys.readouterr().out
    assert _listed(client, robert)["hello"]["version"] == "2" and _listed(client, robert)["hello"]["state"] == "on"

    reads_more = _hello(tmp_path, version="3", internet=True,
                        downloads=[{"url": "https://example.org/words.zip", "title": "Words", "about_mb": 2}])
    _answers(monkeypatch, "n")
    assert cli.main(["plugins", "install", str(reads_more), "--env", env]) == 1
    out = capsys.readouterr().out
    assert "Installed now: version 2. This version also:\n  • Downloads from example.org\n  • Uses the internet" in out
    assert _listed(client, robert)["hello"]["version"] == "2"
    assert json.loads((program_dir(data, "hello") / "lumiverb-plugin.json").read_text())["version"] == "2"
    assert [p.name for p in program_dir(data, "hello").parent.iterdir()] == ["program"], "no copy left behind"


@pytest.mark.slow
def test_what_isnt_a_plug_in_or_isnt_its_own_is_refused(install, machine, tmp_path, capsys):  # noqa: F811
    client, robert, _ = install
    env, data = machine
    for folder, why in (
        (tmp_path / "nowhere", "No folder at"),
        (_hello(tmp_path, "bad", id="Bad", protocol=7), "  • id: lowercase letters"),
    ):
        assert cli.main(["plugins", "install", str(folder), "--off", "--yes", "--env", env]) == 1
        assert why in capsys.readouterr().err
    assert cli.main(["plugins", "install", str(_hello(tmp_path)), "--yes", "--env", env]) == 1
    assert "Say --on or --off" in capsys.readouterr().err
    data.mkdir(parents=True, exist_ok=True)
    assert cli.main(["plugins", "install", str(tmp_path), "--off", "--yes", "--env", env]) == 1
    assert set(_listed(client, robert)) == set()


@pytest.mark.slow
def test_uninstalling_takes_it_away_and_the_log_keeps_everything(install, machine, tmp_path, monkeypatch):  # noqa: F811
    client, robert, _ = install
    env, data = machine
    assert cli.main(["plugins", "install", str(_hello(tmp_path)), "--off", "--yes", "--env", env]) == 0
    assert cli.main(["plugins", "default", "hello", "on", "--env", env]) == 0
    assert cli.main(["plugins", "uninstall", "hello", "--env", env]) == 1, "not said what becomes of what it made"
    _answers(monkeypatch, "n")
    assert cli.main(["plugins", "uninstall", "hello", "--delete", "--env", env]) == 1
    assert "hello" in _listed(client, robert)
    assert cli.main(["plugins", "uninstall", "hello", "--delete", "--yes", "--env", env]) == 0
    assert "hello" not in _listed(client, robert) and not (data / "plugins" / "hello").exists()
    assert client.put("/v1/plugins/hello", json={"enabled": True}, headers=robert).status_code == 404
    assert cli.main(["plugins", "uninstall", "hello", "--delete", "--yes", "--env", env]) == 1
    with get_control_session() as session:
        log = InstalledPluginRepository(session).log("hello")
    assert [(e["action"], e["version"], e["on_by_default"]) for e in log] == [
        ("install", "1", False), ("default", None, True), ("uninstall", "1", None)]
    assert all(e["by"].endswith(" on this machine") for e in log)


@pytest.mark.slow
def test_the_api_says_what_the_runner_says(install, control, short_data, tmp_path, monkeypatch):  # noqa: F811
    """Installed on the machine, run by the runner reading the control database, said by the API."""
    from src.server.plugin_runner import Runner

    client, robert, _ = install
    env = _env_file(tmp_path, control)
    env.write_text(env.read_text() + f"DATA_DIR={short_data}\n")
    monkeypatch.setenv("DATA_DIR", str(short_data))
    get_settings.cache_clear()
    assert cli.main(["plugins", "install", str(_hello(tmp_path)), "--on", "--yes", "--env", str(env)]) == 0
    running = _listed(client, robert)["hello"]["running"]
    assert (running["state"], running["why"]) == ("not_running", "The plug-in runner isn't running")

    runner = Runner(short_data)
    try:
        _until(runner, lambda: _listed(client, robert)["hello"]["running"]["state"] == "ready")
        running = _listed(client, robert)["hello"]["running"]
        assert running["why"] is None and running["restarts"] == 0 and running["since"]
        assert cli.main(["plugins", "uninstall", "hello", "--delete", "--yes", "--env", str(env)]) == 0
        _until(runner, lambda: runner.plugins == {})
    finally:
        runner.stop_all()


def _settings(listed: dict) -> dict:
    return {s["key"]: s for s in listed["settings"]}


@pytest.mark.fast
def test_a_change_names_what_it_changes_and_the_plug_in_is_handed_every_value() -> None:
    from src.plugins.manifest import read
    from src.server.plugin_settings import changed, values

    m = read(HELLO)
    chosen, errors = changed(m, "account", {"times": 2, "gone": 1}, {"greeting": "Hi", "loud": None})
    assert (chosen, errors) == ({"times": 2, "greeting": "Hi"}, {}), "others stay; one it no longer has goes"
    _, errors = changed(m, "account", {}, {"times": 9, "language": "de", "token": "x", "colour": "red"})
    assert errors == {"times": "Times is from 1 to 5", "language": "Language is one of English, French",
                      "token": "The server's: the server admin sets it", "colour": "Not one of its settings"}
    assert values(m, {"times": 3}, {"token": "t"}) == {"greeting": "Hello", "times": 3, "loud": False,
                                                       "language": "en", "token": "t"}


@pytest.mark.slow
def test_its_settings_are_shown_from_its_manifest_and_checked_before_they_are_saved(install, machine, tmp_path):  # noqa: F811
    from tests.real_account import someone

    client, robert, tenant_id = install
    env, _ = machine
    assert cli.main(["plugins", "install", str(_hello(tmp_path)), "--on", "--yes", "--env", env]) == 0
    shown = _settings(_listed(client, robert)["hello"])
    assert list(shown) == ["greeting", "times", "loud", "language", "token"]
    times = shown["times"]
    assert (times["type"], times["value"], times["default"], times["minimum"], times["maximum"], times["set"]) == (
        "int", 1, 1, 1, 5, False)
    assert times["help"] == "How many times it says it." and times["editable"] is True
    assert shown["language"]["choices"] == [{"value": "en", "label": "English"}, {"value": "fr", "label": "French"}]
    assert (shown["token"]["scope"], shown["token"]["value"], shown["token"]["advanced"]) == ("server", None, True)

    # An account's admin who isn't a server admin: the account's settings only.
    other = someone(client, tenant_id, "admin")
    assert "token" not in _settings(_listed(client, other)["hello"])
    r = client.put("/v1/plugins/hello/server-settings", json={"settings": {"token": "t"}}, headers=other)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "server_admin_required")

    r = client.put("/v1/plugins/hello/settings", json={"settings": {"times": 9, "language": "de", "token": "x"}},
                   headers=robert)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_settings", r.text
    assert r.json()["error"]["details"]["errors"] == {
        "times": "Times is from 1 to 5", "language": "Language is one of English, French",
        "token": "The server's: the server admin sets it"}
    r = client.put("/v1/plugins/hello/settings", json={"settings": {"times": 3, "greeting": "Hi"}}, headers=robert)
    assert r.status_code == 200, r.text
    assert r.json()["unchecked"] == "Hello isn't running: its own check didn't run", "saved, unchecked"
    shown = _settings(_listed(client, other)["hello"])
    assert (shown["times"]["value"], shown["times"]["set"], shown["greeting"]["value"]) == (3, True, "Hi")
    r = client.put("/v1/plugins/hello/settings", json={"settings": {"times": None}}, headers=robert)
    assert _settings(r.json())["times"]["value"] == 1 and _settings(r.json())["times"]["set"] is False

    r = client.put("/v1/plugins/hello/server-settings", json={"settings": {"token": "s3cret"}}, headers=robert)
    assert r.status_code == 200, r.text
    assert (_settings(r.json())["token"]["set"], _settings(r.json())["token"]["value"]) == (True, None)
    assert "s3cret" not in client.get("/v1/plugins", headers=robert).text, "a secret is never shown again"
    viewer = someone(client, tenant_id, "viewer")
    assert client.put("/v1/plugins/hello/settings", json={"settings": {"times": 2}}, headers=viewer).status_code == 403
    assert _settings(_listed(client, viewer)["hello"])["times"]["editable"] is False
    for path in ("/v1/plugins/geonames/settings", "/v1/plugins/nope/settings"):
        assert client.put(path, json={"settings": {}}, headers=robert).status_code == 404


@pytest.mark.slow
def test_a_plug_in_that_checks_its_settings_is_asked_while_it_runs(install, control, short_data, tmp_path,  # noqa: F811
                                                                  monkeypatch):
    from src.server.plugin_runner import Runner

    client, robert, _ = install
    env = _env_file(tmp_path, control)
    env.write_text(env.read_text() + f"DATA_DIR={short_data}\n")
    monkeypatch.setenv("DATA_DIR", str(short_data))
    get_settings.cache_clear()
    assert cli.main(["plugins", "install", str(_hello(tmp_path)), "--on", "--yes", "--env", str(env)]) == 0
    runner = Runner(short_data)
    try:
        _until(runner, lambda: _listed(client, robert)["hello"]["running"]["state"] == "ready")
        r = client.put("/v1/plugins/hello/settings", json={"settings": {"greeting": "rude"}}, headers=robert)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["details"] == {"errors": {"greeting": "Not a nice greeting"}, "by_plugin": True}
        assert _settings(_listed(client, robert)["hello"])["greeting"]["value"] == "Hello", "not saved"
        r = client.put("/v1/plugins/hello/settings", json={"settings": {"greeting": "Hi"}}, headers=robert)
        assert r.status_code == 200 and r.json()["unchecked"] is None, r.text
    finally:
        runner.stop_all()


@pytest.mark.slow
def test_one_installed_with_an_id_lumiverb_uses_now_is_left_out_and_said(install, machine, capsys, caplog):  # noqa: F811
    # ADR-024 section 8: installed before part 1 refused such an id (none is on Robert's Mac). Left out, as a
    # manifest Lumiverb no longer reads is: not run, its work not a producer; said on the Admin page, in the log at
    # every start, and by `lumiverb-server plugins`, with what to do.
    import logging

    from src.server import plugin_catalog
    from src.shared.utils import utcnow

    client, robert, _ = install
    env, _ = machine
    manifest = {**json.loads((HELLO / "lumiverb-plugin.json").read_text()), "id": "faces", "title": "Faces Plus"}
    with get_control_session() as session:
        InstalledPluginRepository(session).save("faces", version="1", manifest=manifest, on_by_default=True,
                                                acknowledged_version=None, permissions={}, by="robert", now=utcnow())
        session.commit()
    said = "Faces Plus's id `faces` is one Lumiverb uses now: uninstall it, then install it under another id"
    try:
        plugin_catalog._SAID.discard("faces")  # as a new process starts
        with caplog.at_level(logging.WARNING, logger=plugin_catalog.__name__):
            assert "faces" not in plugin_catalog.installed() and "faces" not in plugin_catalog.installed()
        assert [r.getMessage() for r in caplog.records if "faces" in r.getMessage()] == [
            f"Plug-in faces left out: {said}"], "once a start"
        assert "faces" not in _listed(client, robert)
        r = client.get("/v1/producers", params={"counts": "false"}, headers=robert)
        assert r.status_code == 200 and "faces" not in {p["artifact"] for p in r.json()["producers"]}, r.text
        rows = {r["key"]: r for r in client.get("/v1/system/health", headers=robert).json()["rows"]}
        assert (rows["plugins"]["state"], rows["plugins"]["reason"]) == ("red", f"{said}."), rows.get("plugins")
        assert cli.main(["plugins", "--env", env]) == 0
        assert f"  faces 1  Faces Plus: on by default\n    ! left out: {said}" in capsys.readouterr().out
    finally:
        with get_control_session() as session:
            InstalledPluginRepository(session).remove("faces", "delete", by="robert", now=utcnow())
            session.commit()
    rows = {r["key"]: r for r in client.get("/v1/system/health", headers=robert).json()["rows"]}
    assert "plugins" not in rows, "uninstalled: nothing to say"
