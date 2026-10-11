"""Whether other devices on the network may use a Mac's Lumiverb
(src/server/lifecycle/network.py): an admin's choice in DATA_DIR/network.json,
the listen lines nginx's config includes, and run applying a change."""

from __future__ import annotations

import threading
import time

import pytest

from src.server.lifecycle import network
from src.server.lifecycle.init import nginx_conf, nginx_site

pytestmark = pytest.mark.fast


def test_a_new_install_is_this_machine_only(tmp_path):
    assert network.other_devices(tmp_path) is False
    assert network.write_listen_file(tmp_path, 6464, False) is True
    lines = network.listen_file(tmp_path).read_text()
    assert lines.endswith("listen 127.0.0.1:6464 default_server;\nlisten [::1]:6464 default_server;\n")
    assert lines.startswith("# lumiverb-server run's, from Settings › Network")


def test_other_devices_listens_on_every_address(tmp_path):
    network.set_other_devices(tmp_path, True)
    assert network.other_devices(tmp_path) is True
    assert network.write_listen_file(tmp_path, 6464, True) is True
    assert network.listen_file(tmp_path).read_text().endswith(
        "listen 6464 default_server;\nlisten [::]:6464 default_server;\n")
    assert network.write_listen_file(tmp_path, 6464, True) is False, "unchanged: nothing to restart"
    network.set_other_devices(tmp_path, False)
    assert network.other_devices(tmp_path) is False
    assert not list(tmp_path.glob(".network.json*")), "written whole, through a file of its own"


@pytest.mark.parametrize("text", ["", "not json", "[]", '"yes"'])
def test_a_choice_it_cant_read_is_this_machine_only(tmp_path, text):
    (tmp_path / network.NETWORK_FILE).write_text(text)
    assert network.other_devices(tmp_path) is False


def test_its_nginx_is_managed_only_when_its_config_includes_the_lines(tmp_path):
    def conf(listen: tuple[str, ...]) -> str:
        path = tmp_path / "nginx.conf"
        path.write_text(nginx_conf(nginx_site(listen=listen, server_name="_", root=tmp_path / "web",
                                              upstream="http://127.0.0.1:6465")))
        return str(path)

    assert network.managed(tmp_path, conf((network.include_line(tmp_path),)))
    # An nginx config of its own that names its listen lines (an install from before)
    assert not network.managed(tmp_path, conf(("listen 127.0.0.1:6464 default_server;",)))
    assert not network.managed(tmp_path, None) and not network.managed(tmp_path, "")
    assert not network.managed(tmp_path, str(tmp_path / "missing.conf"))


def test_a_change_rewrites_the_lines_and_restarts_nginx_once(tmp_path):
    network.write_listen_file(tmp_path, 6464, False)
    stop, restarts = threading.Event(), []
    watcher = threading.Thread(target=network.watch, args=(tmp_path, 6464, stop, lambda: restarts.append(1)),
                               kwargs={"every": 0.02})
    watcher.start()
    try:
        time.sleep(0.1)
        assert restarts == [], "nothing changed"
        network.set_other_devices(tmp_path, True)
        deadline = time.monotonic() + 5
        while not restarts:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        time.sleep(0.1)
        assert restarts == [1]
        assert "listen 6464 default_server;" in network.listen_file(tmp_path).read_text()
    finally:
        stop.set()
        watcher.join(5)
    assert not watcher.is_alive()


def test_addresses_are_the_mac_s_name_then_its_address(monkeypatch):
    monkeypatch.setattr(network.socket, "gethostname", lambda: "macstudio")
    monkeypatch.setattr(network, "_lan_address", lambda: "192.168.86.55")
    assert network.addresses(6464) == ["http://macstudio.local:6464", "http://192.168.86.55:6464"]
    monkeypatch.setattr(network.socket, "gethostname", lambda: "macstudio.local")
    monkeypatch.setattr(network, "_lan_address", lambda: None)
    assert network.addresses(6464) == ["http://macstudio.local:6464"], "no network: its name only"
