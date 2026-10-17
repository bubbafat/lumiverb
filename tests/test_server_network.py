"""Whether other devices on the network may use a Mac's Lumiverb
(src/server/lifecycle/network.py): an admin's choice in DATA_DIR/network.json,
the lines nginx's config includes (other devices over plain HTTP, HTTPS
too if they add it, with the certificate tls.py serves, and HTTPS alone once
they turn plain HTTP off), whether a device has used HTTPS, and run applying
a change. The handshake itself: test_server_tls.py; through real nginx:
test_server_run.py."""

from __future__ import annotations

import datetime as dt
import threading
import time

import pytest

from src.server.lifecycle import network
from src.server.lifecycle.init import nginx_conf, nginx_site
from src.server.lifecycle.network import Choice

pytestmark = pytest.mark.fast

OTHERS = Choice(other_devices=True)
WITH_HTTPS = Choice(other_devices=True, https=True)
HTTPS_ALONE = Choice(other_devices=True, https=True, plain_http=False)


def test_a_new_install_is_this_machine_only(tmp_path):
    assert network.choice(tmp_path) == Choice(other_devices=False, https=False, plain_http=True)
    assert network.write_listen_file(tmp_path, 6464, Choice()) is True
    lines = network.listen_file(tmp_path).read_text()
    assert lines.endswith("listen 127.0.0.1:6464 default_server;\nlisten [::1]:6464 default_server;\n")
    assert lines.startswith("# lumiverb-server run's, from Settings › Network")


def _served(tmp_path):
    from src.server.lifecycle import tls

    return tls.served(tmp_path, ("macstudio.local", "localhost", "127.0.0.1"))


def test_other_devices_are_served_plain_http_with_nothing_to_set_up(tmp_path):
    network.set_choice(tmp_path, OTHERS)
    assert network.choice(tmp_path) == OTHERS and OTHERS.http_for_others and not OTHERS.https_for_others
    network.write_listen_file(tmp_path, 6464, OTHERS, tls_port=6465, served=_served(tmp_path))
    lines = network.listen_file(tmp_path).read_text()
    assert "listen 6464 default_server;\nlisten [::]:6464 default_server;\n" in lines
    assert "ssl" not in lines and "301" not in lines and "lumiverb-ca.crt" not in lines
    assert not list(tmp_path.glob(".network.json*")), "written whole, through a file of its own"


def test_https_beside_plain_http(tmp_path):
    from src.server.lifecycle import tls

    served = _served(tmp_path)
    assert network.write_listen_file(tmp_path, 6464, WITH_HTTPS, tls_port=6465, served=served,
                                     ca=tls.ca_certificate(tmp_path)) is True
    lines = network.listen_file(tmp_path).read_text()
    for line in ("listen 6464 default_server;", "listen 6465 ssl default_server;",
                 "listen [::]:6465 ssl default_server;", f"ssl_certificate {served.certificate};",
                 f"ssl_certificate_key {served.key};", "ssl_protocols TLSv1.2 TLSv1.3;",
                 f"sha256 {served.fingerprint}", f"alias {tls.ca_certificate(tmp_path)};"):
        assert line in lines, line
    assert "301" not in lines, "plain HTTP stays every device's"
    assert network.write_listen_file(tmp_path, 6464, WITH_HTTPS, tls_port=6465, served=served,
                                     ca=tls.ca_certificate(tmp_path)) is False, "unchanged: nothing to restart"


def test_https_alone_sends_other_devices_plain_http_to_it_but_for_the_authority(tmp_path):
    from src.server.lifecycle import tls

    served = _served(tmp_path)
    network.write_listen_file(tmp_path, 6464, HTTPS_ALONE, tls_port=6465, served=served,
                              ca=tls.ca_certificate(tmp_path))
    lines = network.listen_file(tmp_path).read_text()
    assert "if ($to_https = AB) { return 301 https://$host:6465$request_uri; }" in lines
    assert "if ($uri = /lumiverb-ca.crt) { set $to_https C; }" in lines
    assert "listen 6464 default_server;" in lines, "still heard, to be sent on"


def test_plain_http_is_off_only_beside_https():
    assert Choice(other_devices=True, plain_http=False).http_for_others, "HTTPS off: plain HTTP is all they have"
    assert not HTTPS_ALONE.http_for_others
    assert not Choice(https=True).https_for_others, "this machine only"
    assert [c.said() for c in (Choice(), OTHERS, WITH_HTTPS, HTTPS_ALONE)] == [
        "this machine only", "other devices over HTTP", "other devices over HTTP and HTTPS",
        "other devices over HTTPS alone"]


def test_this_machine_only_has_neither_https_nor_the_authority(tmp_path):
    network.write_listen_file(tmp_path, 6464, Choice(https=True), tls_port=6465, served=_served(tmp_path))
    lines = network.listen_file(tmp_path).read_text()
    assert "listen 127.0.0.1:6464 default_server;" in lines and "ssl" not in lines
    assert "lumiverb-ca.crt" not in lines


def test_no_certificate_is_made_until_https_is_chosen(tmp_path, monkeypatch):
    from src.server.lifecycle import tls

    monkeypatch.setattr(tls, "names_here", lambda: ("macstudio.local", "localhost", "127.0.0.1"))
    network.set_choice(tmp_path, OTHERS)
    network.apply(tmp_path, 6464, 6465)
    assert not (tmp_path / "tls").exists() and tls.describe(tmp_path) is None
    network.set_choice(tmp_path, WITH_HTTPS)
    assert network.apply(tmp_path, 6464, 6465) is True
    assert tls.describe(tmp_path).source == tls.LUMIVERB


def test_without_https_other_devices_keep_plain_http(tmp_path):
    """run, when the certificate can't be made: the watch tries again."""
    network.set_choice(tmp_path, HTTPS_ALONE)
    network.without_https(tmp_path, 6464)
    lines = network.listen_file(tmp_path).read_text()
    assert "listen 6464 default_server;" in lines and "ssl" not in lines and "301" not in lines
    assert network.choice(tmp_path) == HTTPS_ALONE, "the choice kept"


def test_the_first_device_seen_using_https_is_kept_until_https_changes(tmp_path):
    assert network.https_seen(tmp_path) is None
    first = dt.datetime(2026, 10, 17, 14, 2, tzinfo=dt.UTC)
    assert network.saw_https(tmp_path, "192.168.86.20", first) is True
    assert network.saw_https(tmp_path, "192.168.86.30") is False, "the first is kept"
    assert network.https_seen(tmp_path) == network.Seen(at=first, address="192.168.86.20")
    assert not list(tmp_path.glob(f".{network.HTTPS_SEEN_FILE}*")), "written whole, through a file of its own"
    network.forget_https_seen(tmp_path)
    assert network.https_seen(tmp_path) is None
    network.forget_https_seen(tmp_path)  # none to forget


def test_the_tls_port_is_the_setting_else_6443_and_never_another_of_its_ports():
    assert network.tls_port() == network.tls_port("") == network.tls_port("0") == 6443
    assert network.tls_port("8443") == 8443
    mac = {"WEB_PORT": "6464", "API_PORT": "6465",
           "CONTROL_PLANE_DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:6468/control_plane"}
    assert network.tls_port_taken(mac, 6443) is None
    assert network.tls_port_taken(mac, 6465) == "API_PORT", "a Mac's API: WEB_PORT + 1 was"
    assert network.tls_port_taken(mac, 6464) == "WEB_PORT"
    assert network.tls_port_taken(mac, 6468) == "the database's port"


@pytest.mark.parametrize("text", ["", "not json", "[]", '"yes"'])
def test_a_choice_it_cant_read_is_this_machine_only(tmp_path, text):
    (tmp_path / network.NETWORK_FILE).write_text(text)
    assert network.choice(tmp_path) == Choice()


def test_a_choice_from_before_https_was_optional_is_other_devices_over_plain_http(tmp_path):
    """v0.1.52–v0.1.53 wrote only other_devices, and served them HTTPS alone."""
    (tmp_path / network.NETWORK_FILE).write_text('{"other_devices": true}\n')
    assert network.choice(tmp_path) == OTHERS


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
        network.set_choice(tmp_path, WITH_HTTPS)
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
    network.set_choice(tmp_path, WITH_HTTPS)
    assert network.apply(tmp_path, 6464, 6465) is True
    assert network.apply(tmp_path, 6464, 6465) is False, "nothing changed"
    names[0] = "192.168.86.77"
    assert network.apply(tmp_path, 6464, 6465) is True
    assert "192.168.86.77" in tls.describe(tmp_path).names


def test_addresses_are_the_mac_s_name_then_its_address(monkeypatch):
    monkeypatch.setattr(network.socket, "gethostname", lambda: "macstudio")
    monkeypatch.setattr(network, "lan_address", lambda: "192.168.86.55")
    assert network.addresses(6464, "http") == ["http://macstudio.local:6464", "http://192.168.86.55:6464"]
    assert network.addresses(6465, "https") == ["https://macstudio.local:6465", "https://192.168.86.55:6465"]
    monkeypatch.setattr(network.socket, "gethostname", lambda: "macstudio.local")
    monkeypatch.setattr(network, "lan_address", lambda: None)
    assert network.addresses(6465, "https") == ["https://macstudio.local:6465"], "no network: its name only"
