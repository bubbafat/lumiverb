"""Settings › Network's API (GET/PUT /v1/system/network, routers/system.py):
whether other devices on the network may use a Mac's Lumiverb (over plain
HTTP, HTTPS too, or HTTPS alone once a device has used it), where they reach
it, and the certificate HTTPS serves (an admin's own, PUT/DELETE
/v1/system/network/certificate). Anyone signed in sees it; an admin changes
it, on the Mac itself, and only where Lumiverb runs the web server
(lumiverb-server run's own nginx). run applies it (tests/test_server_run.py)."""

from __future__ import annotations

import bcrypt
import pytest
from fastapi.testclient import TestClient

from src.server.api import rate_limit
from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import get_control_session
from src.server.lifecycle import network
from src.server.lifecycle.init import nginx_conf, nginx_site
from src.server.lifecycle.network import Choice
from src.server.repository.control_plane import UserRepository
from src.server.setup import create_first_admin
from tests.test_first_run_setup import (  # noqa: F401 (control: a fixture)
    HERE,
    LAN,
    LOCALHOST,
    PASSWORD,
    control,
)

pytestmark = pytest.mark.slow

HTTP = ["http://macstudio.local:6464", "http://192.168.86.55:6464"]
HTTPS = ["https://macstudio.local:6465", "https://192.168.86.55:6465"]
# A device's request as nginx passes it on: from this machine, saying what it saw.
THROUGH_NGINX_OVER_HTTPS = {"X-Forwarded-For": LAN[0], "X-Forwarded-Proto": "https"}


@pytest.fixture
def mac(control, tmp_path, monkeypatch):  # noqa: F811
    """A Mac's install: its own nginx, whose config includes the listen lines."""
    conf = tmp_path / "nginx.conf"
    conf.write_text(nginx_conf(nginx_site(listen=(network.include_line(tmp_path),), server_name="_",
                                          root=tmp_path / "web", upstream="http://127.0.0.1:6465")))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NGINX_CONFIG", str(conf))
    monkeypatch.setenv("WEB_PORT", "6464")
    monkeypatch.setenv("API_PORT", "6364")
    monkeypatch.setattr(network, "addresses", lambda port, scheme: [
        f"{scheme}://macstudio.local:{port}", f"{scheme}://192.168.86.55:{port}"])
    monkeypatch.setattr(rate_limit.login_limiter, "_hits", {})
    # Its own ports are this pretend Mac's (the machine running the tests may use them); others, as they are.
    free = network.free
    monkeypatch.setattr(network, "free", lambda port: port in (6464, 6465) or free(port))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _signed_in(client: TestClient, role: str = "admin") -> dict[str, str]:
    admin = create_first_admin("robert@example.com", PASSWORD)
    email = "robert@example.com"
    if role != "admin":
        email = f"{role}@example.com"
        with get_control_session() as session:
            UserRepository(session).create(admin.tenant_id, email, bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(),
                                           role=role)
    token = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_a_mac_starts_as_this_machine_only_and_an_admin_there_opens_it_over_plain_http(mac):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        assert client.get("/v1/system/network", headers=auth).json() == {
            "available": True, "other_devices": False, "https": False, "plain_http": True,
            "addresses": HTTP, "https_addresses": HTTPS, "https_seen": None, "certificate": None,
            "http_port": 6464, "https_port": 6465}
        opened = client.put("/v1/system/network", json={"other_devices": True}, headers=auth)
        assert opened.status_code == 200, opened.text
        assert (opened.json()["other_devices"], opened.json()["https"], opened.json()["plain_http"]) == (True, False, True)
        assert network.choice(mac) == Choice(other_devices=True), "written for run to apply"
        closed = client.put("/v1/system/network", json={"other_devices": False}, headers=auth)
        assert closed.json()["other_devices"] is False and network.choice(mac) == Choice()


def test_https_is_added_beside_plain_http_and_what_isn_t_said_stays(mac):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        client.put("/v1/system/network", json={"other_devices": True}, headers=auth)
        added = client.put("/v1/system/network", json={"https": True}, headers=auth)
        assert added.status_code == 200, added.text
    assert network.choice(mac) == Choice(other_devices=True, https=True, plain_http=True)


def test_plain_http_is_turned_off_only_once_a_device_has_used_https(mac):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        client.put("/v1/system/network", json={"other_devices": True, "https": True}, headers=auth)
        refused = client.put("/v1/system/network", json={"plain_http": False}, headers=auth)
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "https_not_seen"
        assert network.choice(mac).plain_http, "unchanged"

        assert client.get("/health", headers=THROUGH_NGINX_OVER_HTTPS).status_code == 200
        seen = client.get("/v1/system/network", headers=auth).json()["https_seen"]
        assert seen["address"] == LAN[0] and seen["at"]

        off = client.put("/v1/system/network", json={"plain_http": False}, headers=auth)
        assert off.status_code == 200, off.text
        assert network.choice(mac) == Choice(other_devices=True, https=True, plain_http=False)

        # HTTPS off: plain HTTP is all other devices have, and a device must use HTTPS again.
        back = client.put("/v1/system/network", json={"https": False}, headers=auth)
        assert (back.json()["https"], back.json()["plain_http"], back.json()["https_seen"]) == (False, True, None)
        assert network.https_seen(mac) is None


def test_turning_on_https_and_off_plain_http_at_once_is_refused(mac):
    network.saw_https(mac, LAN[0])  # from before: HTTPS is new again
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        refused = client.put("/v1/system/network", json={"other_devices": True, "https": True, "plain_http": False},
                             headers=auth)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "https_not_seen"
    assert network.choice(mac) == Choice()


@pytest.mark.parametrize(("client_at", "headers"), [
    (LAN, {"X-Forwarded-Proto": "https"}),  # not through nginx: it says what it likes
    (HERE, {"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"}),  # this machine's own
    (HERE, {"X-Forwarded-For": LAN[0], "X-Forwarded-Proto": "http"}),  # plain HTTP
])
def test_only_a_device_s_request_over_https_through_nginx_counts(mac, client_at, headers):
    network.set_choice(mac, Choice(other_devices=True, https=True))
    with TestClient(app, client=client_at) as client:
        client.get("/health", headers=headers)
    assert network.https_seen(mac) is None


def test_another_certificate_must_be_used_again(mac):
    from tests.test_server_tls import _theirs

    network.set_choice(mac, Choice(other_devices=True, https=True))
    network.saw_https(mac, LAN[0])
    pem, key, _ = _theirs()
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        assert client.put("/v1/system/network/certificate", json={"certificate": pem, "key": key},
                          headers=auth).json()["https_seen"] is None


def test_from_another_device_it_isn_t_changed(mac):
    """The Mac's owner decides how it's reached, as for a library's folder."""
    with TestClient(app, client=HERE, base_url=LOCALHOST) as here:
        auth = _signed_in(here)
    network.set_choice(mac, Choice(other_devices=True))
    with TestClient(app, client=LAN) as client:
        assert client.get("/v1/system/network", headers=auth).json()["other_devices"] is True
        refused = client.put("/v1/system/network", json={"other_devices": False}, headers=auth)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "network_elsewhere"
    assert network.choice(mac).other_devices, "unchanged"


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_others_see_it_but_only_admins_change_it(mac, role):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client, role)
        assert client.get("/v1/system/network", headers=auth).json()["available"] is True
        assert client.put("/v1/system/network", json={"other_devices": True}, headers=auth).status_code == 403
    assert network.choice(mac) == Choice()


def test_signed_out_it_isn_t_said(mac):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        assert client.get("/v1/system/network").status_code == 401


def test_where_lumiverb_doesn_t_run_the_web_server_it_isn_t_changed_here(control, tmp_path, monkeypatch):  # noqa: F811
    """A Linux install's nginx is deploy-web.sh's: no NGINX_CONFIG of run's."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(rate_limit.login_limiter, "_hits", {})
    get_settings.cache_clear()
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        assert client.get("/v1/system/network", headers=auth).json() == {
            "available": False, "other_devices": False, "https": False, "plain_http": True, "addresses": [],
            "https_addresses": [], "https_seen": None, "certificate": None, "http_port": None, "https_port": None}
        refused = client.put("/v1/system/network", json={"other_devices": True}, headers=auth)
        client.get("/health", headers=THROUGH_NGINX_OVER_HTTPS)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "network_not_here"
    assert not (tmp_path / network.NETWORK_FILE).exists()
    assert not (tmp_path / network.HTTPS_SEEN_FILE).exists(), "nothing noted for an nginx that isn't run's"


def test_it_says_which_certificate_https_serves(mac):
    from src.server.lifecycle import tls

    served = tls.served(mac, ("macstudio.local", "localhost", "127.0.0.1"))
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        assert client.get("/v1/system/network", headers=auth).json()["certificate"] is None, "HTTPS is off"
        network.set_choice(mac, Choice(other_devices=True, https=True))
        said = client.get("/v1/system/network", headers=auth).json()["certificate"]
    assert said["source"] == "lumiverb" and said["ca_url"] == "/lumiverb-ca.crt"
    assert said["names"] == list(served.names)
    # Plain HTTP for it (a device can't trust HTTPS yet), and its fingerprint to compare there.
    assert said["ca_addresses"] == ["http://macstudio.local:6464/lumiverb-ca.crt", "http://192.168.86.55:6464/lumiverb-ca.crt"]
    assert said["ca_fingerprint"] == tls.ca_fingerprint(mac) and len(said["ca_fingerprint"].split(":")) == 32
    assert said["expires_at"].startswith(served.not_after.strftime("%Y-%m-%dT%H:%M"))


def test_an_admin_s_own_certificate_is_kept_there_and_taken_away_again(mac):
    from tests.test_server_tls import _theirs

    network.set_choice(mac, Choice(other_devices=True, https=True))
    pem, key, _ = _theirs()
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        kept = client.put("/v1/system/network/certificate", json={"certificate": pem, "key": key}, headers=auth)
        assert kept.status_code == 200, kept.text
        assert kept.json()["certificate"]["source"] == "yours"
        assert kept.json()["certificate"]["names"] == ["photos.example.com"] and kept.json()["certificate"]["ca_url"] is None
        bad = client.put("/v1/system/network/certificate", json={"certificate": pem, "key": _theirs()[1]}, headers=auth)
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "certificate_not_usable"
        assert "isn't the certificate's" in bad.json()["error"]["message"]
        gone = client.delete("/v1/system/network/certificate", headers=auth)
        assert gone.status_code == 200 and gone.json()["certificate"] is None, "none of Lumiverb's made yet: run makes it"
    assert not (mac / "tls" / "yours.key").exists()


def test_an_admin_s_certificate_isn_t_taken_from_another_device_or_by_an_editor(mac):
    from tests.test_server_tls import _theirs

    pem, key, _ = _theirs()
    with TestClient(app, client=HERE, base_url=LOCALHOST) as here:
        auth = _signed_in(here)
    with TestClient(app, client=LAN) as client:
        refused = client.put("/v1/system/network/certificate", json={"certificate": pem, "key": key}, headers=auth)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "network_elsewhere"
    assert not (mac / "tls" / "yours.crt").exists()


def test_an_admin_there_chooses_its_ports(mac):
    from tests.test_server_postgres import free_port

    http, https = free_port(), free_port()
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        moved = client.put("/v1/system/network", json={"other_devices": True, "http_port": http, "https_port": https},
                           headers=auth)
        assert moved.status_code == 200, moved.text
        said = moved.json()
    assert (said["http_port"], said["https_port"]) == (http, https)
    assert said["addresses"] == [f"http://macstudio.local:{http}", f"http://192.168.86.55:{http}"]
    assert said["https_addresses"][0] == f"https://macstudio.local:{https}"
    assert network.ports(mac, network.Ports()) == network.Ports(http, https), "written for run to apply"
    assert network.choice(mac) == Choice(other_devices=True)


def _refused(mac, change: dict) -> str:
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        refused = client.put("/v1/system/network", json=change, headers=auth)
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "port_not_usable", refused.text
    assert network.ports(mac, network.Ports()) == network.Ports() and network.choice(mac) == Choice(), "unchanged"
    return refused.json()["error"]["message"]


@pytest.mark.parametrize(("change", "says"), [
    ({"http_port": 6465}, "HTTP and HTTPS can't share port 6465"),
    ({"https_port": 6364}, "HTTPS's port 6364 is the API's port"),
    ({"http_port": 70000}, "70000 isn't a port"),
    ({"other_devices": True, "https": True, "https_port": 6364}, "the API's port"),
])
def test_a_port_nginx_can_t_listen_on_is_refused_saying_why(mac, change, says):
    assert says in _refused(mac, change)


def test_a_port_something_else_on_the_mac_uses_is_refused(mac):
    import socket

    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        taken = other.getsockname()[1]
        assert f"Something else on this Mac is using port {taken}" in _refused(mac, {"http_port": taken})


def test_https_on_a_port_something_else_uses_is_refused(mac):
    """Not nginx's while HTTPS is off: asked about when it's turned on."""
    import socket

    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        taken = other.getsockname()[1]
        network.set_ports(mac, network.Ports(6464, taken))
        with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
            auth = _signed_in(client)
            refused = client.put("/v1/system/network", json={"other_devices": True, "https": True}, headers=auth)
    assert refused.status_code == 422 and f"using port {taken}" in refused.json()["error"]["message"]
    assert network.choice(mac) == Choice()


def test_ports_aren_t_chosen_from_another_device(mac):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as here:
        auth = _signed_in(here)
    with TestClient(app, client=LAN) as client:
        refused = client.put("/v1/system/network", json={"http_port": 18080}, headers=auth)
    assert refused.status_code == 403 and network.ports(mac, network.Ports()) == network.Ports()
