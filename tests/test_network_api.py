"""Settings › Network's API (GET/PUT /v1/system/network, routers/system.py):
whether other devices on the network may use a Mac's Lumiverb, where they
reach it (over HTTPS) and the certificate they're served (an admin's own,
PUT/DELETE /v1/system/network/certificate). Anyone signed in sees it; an
admin changes it, on the Mac itself, and only where Lumiverb runs the web
server (lumiverb-server run's own nginx). run applies it
(tests/test_server_run.py)."""

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

ADDRESSES = ["https://macstudio.local:6443", "https://192.168.86.55:6443"]


@pytest.fixture
def mac(control, tmp_path, monkeypatch):  # noqa: F811
    """A Mac's install: its own nginx, whose config includes the listen lines."""
    conf = tmp_path / "nginx.conf"
    conf.write_text(nginx_conf(nginx_site(listen=(network.include_line(tmp_path),), server_name="_",
                                          root=tmp_path / "web", upstream="http://127.0.0.1:6465")))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NGINX_CONFIG", str(conf))
    monkeypatch.setenv("WEB_PORT", "6464")
    monkeypatch.setattr(network, "addresses", lambda port, scheme="https": [
        a.replace("6443", str(port)).replace("https", scheme) for a in ADDRESSES])
    monkeypatch.setattr(rate_limit.login_limiter, "_hits", {})
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


def test_a_mac_starts_as_this_machine_only_and_an_admin_there_opens_it(mac):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        assert client.get("/v1/system/network", headers=auth).json() == {
            "available": True, "other_devices": False, "addresses": ADDRESSES, "certificate": None}
        opened = client.put("/v1/system/network", json={"other_devices": True}, headers=auth)
        assert opened.status_code == 200, opened.text
        assert opened.json()["other_devices"] is True
        assert network.other_devices(mac), "written for run to apply"
        assert client.get("/v1/system/network", headers=auth).json()["other_devices"] is True
        closed = client.put("/v1/system/network", json={"other_devices": False}, headers=auth)
        assert closed.json()["other_devices"] is False and not network.other_devices(mac)


def test_from_another_device_it_isn_t_changed(mac):
    """The Mac's owner decides how it's reached, as for a library's folder."""
    with TestClient(app, client=HERE, base_url=LOCALHOST) as here:
        auth = _signed_in(here)
    network.set_other_devices(mac, True)
    with TestClient(app, client=LAN) as client:
        assert client.get("/v1/system/network", headers=auth).json()["other_devices"] is True
        refused = client.put("/v1/system/network", json={"other_devices": False}, headers=auth)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "network_elsewhere"
    assert network.other_devices(mac), "unchanged"


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_others_see_it_but_only_admins_change_it(mac, role):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client, role)
        assert client.get("/v1/system/network", headers=auth).json()["available"] is True
        assert client.put("/v1/system/network", json={"other_devices": True}, headers=auth).status_code == 403
    assert not network.other_devices(mac)


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
            "available": False, "other_devices": False, "addresses": [], "certificate": None}
        refused = client.put("/v1/system/network", json={"other_devices": True}, headers=auth)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "network_not_here"
    assert not (tmp_path / network.NETWORK_FILE).exists()


def test_it_says_which_certificate_other_devices_are_served(mac):
    from src.server.lifecycle import tls

    served = tls.served(mac, ("macstudio.local", "localhost", "127.0.0.1"))
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth = _signed_in(client)
        said = client.get("/v1/system/network", headers=auth).json()["certificate"]
    assert said["source"] == "lumiverb" and said["ca_url"] == "/lumiverb-ca.crt"
    assert said["names"] == list(served.names)
    # Plain HTTP for it alone (a device can't trust HTTPS yet), and its fingerprint to compare there.
    assert said["ca_addresses"] == ["http://macstudio.local:6464/lumiverb-ca.crt", "http://192.168.86.55:6464/lumiverb-ca.crt"]
    assert said["ca_fingerprint"] == tls.ca_fingerprint(mac) and len(said["ca_fingerprint"].split(":")) == 32
    assert said["expires_at"].startswith(served.not_after.strftime("%Y-%m-%dT%H:%M"))


def test_an_admin_s_own_certificate_is_kept_there_and_taken_away_again(mac):
    from tests.test_server_tls import _theirs

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
