"""What a server answers from anywhere but its own machine
(src/server/api/exposure.py, LUMIVERB_EXPOSURE): with "demo", every route
of the app but the ones its list names answers 404 from elsewhere, decided by
the route the app would run, before anything else; from the machine itself
(an SSH tunnel's too) every route is reached. What says where a request
comes from is client_address.py's: a header from elsewhere counts for
nothing."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.responses import JSONResponse

from src.server.api.exposure import DEMO, EXPOSURES, ExposureGate, route_of
from src.server.api.main import app
from src.server.config import Settings, get_settings
from tests.app_routes import api_routes

pytestmark = pytest.mark.fast

HERE = ("127.0.0.1", 50000)
VISITOR = "203.0.113.9"
THROUGH_NGINX = {"X-Forwarded-For": VISITOR}  # nginx's $proxy_add_x_forwarded_for, on its connection from here


async def _reached(scope, receive, send) -> None:
    await JSONResponse({"reached": True})(scope, receive, send)


def _client(address=HERE, base_url: str = "https://try.lumiverb.io") -> TestClient:
    """The gate in front of a stand-in for the rest, with the app's own routes to decide by."""
    gate = ExposureGate(_reached, headers={"X-Frame-Options": "DENY"})

    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            scope["app"] = app  # as Starlette sets it
        await gate(scope, receive, send)

    return TestClient(asgi, client=address, base_url=base_url)


@pytest.fixture
def exposure(monkeypatch):
    def set_to(value: str) -> None:
        monkeypatch.setenv("LUMIVERB_EXPOSURE", value)
        get_settings.cache_clear()

    yield set_to
    monkeypatch.delenv("LUMIVERB_EXPOSURE", raising=False)
    get_settings.cache_clear()


def _every_route() -> list[tuple[str, str, str]]:
    """(method, route, a path to it) for every route of the app."""
    return [(method, path, re.sub(r"\{[^}]+\}", "x", path))
            for path, methods in api_routes(app) for method in sorted(methods - {"HEAD"})]


def _reaches(client: TestClient, method: str, path: str, headers: dict | None = None) -> bool:
    r = client.request(method, path, headers=headers or {})
    if r.json() == {"reached": True}:
        return True
    assert (r.status_code, r.json()["error"]["code"], r.headers["X-Frame-Options"]) == (404, "not_found", "DENY")
    return False


def test_the_demo_lists_routes_the_app_has_and_reads_but_for_its_sessions_own():
    routes = {(method, path) for path, methods in api_routes(app) for method in methods}
    assert not DEMO - routes, sorted(DEMO - routes)
    assert {entry for entry in DEMO if entry[0] != "GET"} == {
        ("POST", "/v1/auth/demo"), ("POST", "/v1/auth/refresh"), ("POST", "/v1/auth/logout")}
    for left_out in (("POST", "/v1/auth/login"), ("POST", "/v1/auth/forgot-password"), ("POST", "/v1/setup"),
                     ("GET", "/v1/ai"), ("GET", "/v1/system/network"), ("GET", "/v1/producers")):
        assert left_out in routes and left_out not in DEMO, left_out


def test_from_elsewhere_only_the_listed_routes_are_reached(exposure, caplog):
    exposure("demo")
    client = _client()
    caplog.set_level(logging.INFO, logger="src.server.api.exposure")
    for method, route, path in _every_route():
        assert route_of(app, {"type": "http", "method": method, "path": path, "root_path": "",
                              "query_string": b"", "headers": []}) == route, (method, path)
        caplog.clear()
        listed = (method, route) in DEMO
        assert _reaches(client, method, path, THROUGH_NGINX) is listed, (method, route)
        assert ("not exposed here" in caplog.text) is not listed, (method, route, caplog.text)
    assert client.head("/v1/libraries/x", headers=THROUGH_NGINX).status_code == 404, "no route takes HEAD"
    assert _reaches(client, "GET", "/wp-login.php", THROUGH_NGINX) is False, "no route: 404, as ever"


def test_a_route_listed_is_that_route_alone(exposure):
    exposure("demo")
    client = _client()
    # /v1/assets/{asset_id} is listed; the static routes beside it that match first aren't.
    assert _reaches(client, "GET", "/v1/assets/ast_1", THROUGH_NGINX)
    assert not _reaches(client, "GET", "/v1/assets/page", THROUGH_NGINX)
    assert not _reaches(client, "DELETE", "/v1/assets/ast_1", THROUGH_NGINX)
    assert not _reaches(client, "GET", "/v1/assets/ast_1/../../admin/tenants", THROUGH_NGINX)


def test_from_the_machine_itself_every_route_is_reached(exposure):
    exposure("demo")
    here = _client(base_url="http://localhost:6464")  # a browser on it, or an SSH tunnel to it
    for method, route, path in _every_route():
        assert _reaches(here, method, path), (method, route)
        assert _reaches(here, method, path, {"X-Forwarded-For": "127.0.0.1"}), (method, route)


def test_where_a_request_comes_from_is_never_taken_from_what_it_says(exposure):
    exposure("demo")
    # From elsewhere through nginx, naming this machine: still from elsewhere.
    named_here = _client(base_url="http://localhost:6464")
    assert not _reaches(named_here, "GET", "/v1/ai", {"X-Forwarded-For": f"127.0.0.1, {VISITOR}"})
    # Straight to the API from elsewhere, saying it's from here: its own address.
    direct = _client(address=(VISITOR, 50000), base_url="http://localhost:6464")
    assert not _reaches(direct, "GET", "/v1/ai", {"X-Forwarded-For": "127.0.0.1"})
    # From here, by a page from anywhere else.
    assert not _reaches(named_here, "GET", "/v1/ai", {"Origin": "https://try.lumiverb.io"})


def test_unset_every_route_is_reached_from_anywhere(exposure):
    exposure("")
    client = _client()
    for method, route, path in _every_route():
        assert _reaches(client, method, path, THROUGH_NGINX), (method, route)


def test_an_exposure_there_isnt_stops_the_server(exposure):
    with pytest.raises(ValidationError, match="LUMIVERB_EXPOSURE='dmeo' isn't one"):
        Settings(control_plane_database_url="x", tenant_database_url_template="x", lumiverb_exposure="dmeo")


def test_the_install_takes_the_exposures_there_are():
    # deploy-api.sh --exposure checks its name before writing it: the same names the API starts with.
    script = (Path(__file__).resolve().parents[1] / "scripts" / "deploy-api.sh").read_text()
    names = script.split('case "$EXPOSURE" in\n', 1)[1].split(")", 1)[0].strip()
    assert set(names.split("|")) - {'""'} == set(EXPOSURES)
