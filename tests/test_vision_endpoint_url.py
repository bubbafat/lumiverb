"""Which AI machine URLs the server will ask: plain http(s) on loopback or
the LAN (where ollama runs), never link-local or cloud metadata, and one
message for every failure so the answer can't map a network."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.shared.vision_endpoint import UNREACHABLE, VisionEndpointError, check_url, list_models

pytestmark = pytest.mark.fast


def _resolves_to(*ips: str):
    def getaddrinfo(host, port, *_args, **_kwargs):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in ips]
    return patch("src.shared.vision_endpoint.socket.getaddrinfo", side_effect=getaddrinfo)


def _answers(*models: str):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": [{"id": m} for m in models]}
    return patch("src.shared.vision_endpoint.requests.get", return_value=resp)


# The machines on dev and the shapes ollama has on prod (same host, LAN).
@pytest.mark.parametrize("url", [
    "http://172.18.0.6:11434/v1", "http://10.10.10.1:11434/v1", "http://localhost:11434/v1",
    "http://127.0.0.1:11434/v1", "http://[::1]:11434/v1", "http://192.168.1.20:11434/v1/", "https://10.0.0.7/v1",
])
def test_loopback_and_lan_machines_are_asked(url):
    with _answers("qwen3-vl:8b") as get:
        assert list_models(url) == ["qwen3-vl:8b"]
    assert get.call_args.args[0] == url.rstrip("/") + "/models"


@pytest.mark.parametrize("url", [
    "http://h/v1?x=1", "http://h/v1#frag", "http://user@h/v1", "http://user:pw@h/v1", "file:///etc/passwd",
    "gopher://h/", "http://:80/v1", "http://h:99999/v1",
])
def test_a_url_that_isnt_plain_http_is_refused_before_any_request(url):
    with _answers("m") as get, pytest.raises(VisionEndpointError):
        list_models(url)
    get.assert_not_called()
    with pytest.raises(VisionEndpointError):
        check_url(url)


@pytest.mark.parametrize("ip", ["169.254.169.254", "169.254.1.1", "fe80::1", "fe80::1%eth0", "fd00:ec2::254",
                                "::ffff:169.254.169.254"])
def test_link_local_and_metadata_addresses_are_never_asked(ip):
    with _resolves_to("10.0.0.5", ip), _answers("m") as get, pytest.raises(VisionEndpointError) as e:
        list_models("http://sneaky.example/v1")
    get.assert_not_called()
    assert str(e.value) == "That address isn't allowed."


def test_a_metadata_address_typed_as_an_ip_is_refused():
    with _answers("m") as get, pytest.raises(VisionEndpointError):
        list_models("http://169.254.169.254/latest")
    get.assert_not_called()


@pytest.mark.parametrize("fail", [
    requests.ConnectionError("Connection refused by 10.0.0.9:22"), requests.Timeout("10.0.0.9"),
    401, 404, 500, "not json", (),
])
def test_every_failure_says_the_same_and_logs_why(fail, caplog):
    resp = MagicMock(status_code=fail if isinstance(fail, int) else 200)
    if fail == "not json":
        resp.json.side_effect = ValueError("no")
    else:
        resp.json.return_value = {"data": []}
    get = patch("src.shared.vision_endpoint.requests.get",
                **({"side_effect": fail} if isinstance(fail, Exception) else {"return_value": resp}))
    with get, caplog.at_level("WARNING", logger="src.shared.vision_endpoint"), \
            pytest.raises(VisionEndpointError) as e:
        list_models("http://10.0.0.9:22/v1")
    assert str(e.value) == UNREACHABLE
    assert "10.0.0.9:22" in caplog.text


def test_a_host_that_doesnt_resolve_says_the_same():
    with patch("src.shared.vision_endpoint.socket.getaddrinfo", side_effect=socket.gaierror("nope")), \
            pytest.raises(VisionEndpointError) as e:
        list_models("http://nowhere.invalid/v1")
    assert str(e.value) == UNREACHABLE
