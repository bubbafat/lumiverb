"""Whether other devices on the network may use a Mac's Lumiverb
(src/server/lifecycle/network.py): an admin's choice in DATA_DIR/network.json,
the lines nginx's config includes (other devices over HTTPS alone, with the
certificate tls.py serves), and run applying a change. The handshake itself:
test_server_tls.py; through real nginx: test_server_run.py."""

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


def _served(tmp_path):
    from src.server.lifecycle import tls

    return tls.served(tmp_path, ("macstudio.local", "localhost", "127.0.0.1"))


def test_other_devices_are_served_over_https_alone(tmp_path):
    from src.server.lifecycle import tls

    served = _served(tmp_path)
    network.set_other_devices(tmp_path, True)
    assert network.other_devices(tmp_path) is True
    assert network.write_listen_file(tmp_path, 6464, True, tls_port=6465, served=served,
                                     ca=tls.ca_certificate(tmp_path)) is True
    lines = network.listen_file(tmp_path).read_text()
    for line in ("listen 6465 ssl default_server;", "listen [::]:6465 ssl default_server;",
                 f"ssl_certificate {served.certificate};", f"ssl_certificate_key {served.key};",
                 "ssl_protocols TLSv1.2 TLSv1.3;", f"sha256 {served.fingerprint}",
                 "if ($to_https = AB) { return 301 https://$host:6465$request_uri; }",
                 f"alias {tls.ca_certificate(tmp_path)};"):
        assert line in lines, line
    assert network.write_listen_file(tmp_path, 6464, True, tls_port=6465, served=served,
                                     ca=tls.ca_certificate(tmp_path)) is False, "unchanged: nothing to restart"
    network.set_other_devices(tmp_path, False)
    assert network.other_devices(tmp_path) is False
    assert not list(tmp_path.glob(".network.json*")), "written whole, through a file of its own"


def test_this_machine_only_has_no_https_but_offers_the_authority(tmp_path):
    from src.server.lifecycle import tls

    network.write_listen_file(tmp_path, 6464, False, tls_port=6465, served=_served(tmp_path),
                              ca=tls.ca_certificate(tmp_path))
    lines = network.listen_file(tmp_path).read_text()
    assert "listen 127.0.0.1:6464 default_server;" in lines and "ssl" not in lines
    assert "location = /lumiverb-ca.crt" in lines


def test_the_tls_port_is_the_setting_else_the_next_one():
    assert network.tls_port(6464) == network.tls_port(6464, "") == network.tls_port(6464, "0") == 6465
    assert network.tls_port(6464, "8443") == 8443


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


def test_a_change_rewrites_the_lines_and_restarts_nginx_once(tmp_path, monkeypatch):
    from src.server.lifecycle import tls

    monkeypatch.setattr(tls, "names_here", lambda: ("macstudio.local", "localhost", "127.0.0.1"))
    network.apply(tmp_path, 6464, 6465)
    stop, restarts = threading.Event(), []
    watcher = threading.Thread(target=network.watch, args=(tmp_path, 6464, 6465, stop, lambda: restarts.append(1)),
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
        assert "listen 6465 ssl default_server;" in network.listen_file(tmp_path).read_text()
    finally:
        stop.set()
        watcher.join(5)
    assert not watcher.is_alive()


def test_a_new_address_from_the_router_issues_the_certificate_again_and_restarts_nginx(tmp_path, monkeypatch):
    from src.server.lifecycle import tls

    names = ["192.168.86.55"]
    monkeypatch.setattr(tls, "names_here", lambda: ("macstudio.local", names[0], "localhost"))
    network.set_other_devices(tmp_path, True)
    assert network.apply(tmp_path, 6464, 6465) is True
    assert network.apply(tmp_path, 6464, 6465) is False, "nothing changed"
    names[0] = "192.168.86.77"
    assert network.apply(tmp_path, 6464, 6465) is True
    assert "192.168.86.77" in tls.describe(tmp_path).names


def test_addresses_are_https_the_mac_s_name_then_its_address(monkeypatch):
    monkeypatch.setattr(network.socket, "gethostname", lambda: "macstudio")
    monkeypatch.setattr(network, "lan_address", lambda: "192.168.86.55")
    assert network.addresses(6465) == ["https://macstudio.local:6465", "https://192.168.86.55:6465"]
    monkeypatch.setattr(network.socket, "gethostname", lambda: "macstudio.local")
    monkeypatch.setattr(network, "lan_address", lambda: None)
    assert network.addresses(6465) == ["https://macstudio.local:6465"], "no network: its name only"
