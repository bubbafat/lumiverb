# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Text that isn't text in anything a request sends is a 400 not_text, never a
500 (src/server/api/no_nul.py): a NUL character, a lone surrogate (half of a
UTF-16 pair), or a URL whose bytes aren't UTF-8.

PostgreSQL keeps neither a NUL nor a lone surrogate, so one that reached a
query was the database's error, a 500. The URL is refused before sign-in:
every route of the app, signed in or not, with one in a query parameter or
the path, and the public pages' lookups, which never see one (no database
here: the gate answers first). A body after sign-in: a JSON body holding
one, sent to every route against the real API and database, and every form's
text fields; a file's bytes are never read for it, nor a request sign-in or
the demo turns away. One that slips past is still the same 400: the
database's own refusals (psycopg2's ValueError and UnicodeEncodeError,
jsonb's 22P05 and 22P02), through a route that reads a body it isn't told is
JSON.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid

import pytest
from fastapi import params
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DataError
from starlette.requests import Request

from src.server.api import no_nul
from src.server.api.errors import NOT_TEXT_SAYS
from src.server.api.main import SECURITY_HEADERS, app
from src.server.config import get_settings
from tests.app_routes import api_routes
from tests.clips import make_clip
from tests.machine_lineage import made
from tests.test_scans_api import File, _ingest, _library, env  # noqa: F401 — the shared fixture
from tests.videos import playable

NUL = "a\x00b"
HALF = "a\ud800b"  # a lone surrogate: half a character
# In a URL: a surrogate's own bytes, and a byte no UTF-8 has.
NOT_UTF8 = ("a%ED%A0%80b", "%FF")
JPEG_WITH_NULS = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 64


def _refused(r, where: str | None, found: str = "nul") -> None:
    assert r.status_code == 400, (r.status_code, r.text)
    error = r.json()["error"]
    assert error["code"] == "not_text", r.text
    assert error["details"] == {"found": found, **({"in": where} if where else {})}, r.text


def _routes() -> list[tuple[str, str, APIRoute]]:
    """(method, path, route) for every route of the app: each one api_routes lists."""
    walked = [(method, context.path, context.original_route) for context in iter_route_contexts(app.routes)
              if isinstance(context.original_route, APIRoute)
              for method in sorted(set(context.methods or ()) - {"HEAD", "OPTIONS"})]
    listed = {(method, path) for path, methods in api_routes(app) for method in methods - {"HEAD", "OPTIONS"}}
    assert {(method, path) for method, path, _ in walked} == listed and len(walked) > 250, len(walked)
    return walked


def _filled(path: str, value: str = "nope") -> str:
    return re.sub(r"\{(\w+)(:\w+)?\}", lambda m: "lumiverb.preview" if m.group(1) == "artifact_type" else value, path)


def _query_names(dependant) -> set[str]:
    """Every query parameter a route reads, its dependencies' too."""
    names = {param.alias for param in dependant.query_params}
    for sub in dependant.dependencies:
        names |= _query_names(sub)
    return names


def _takes_a_form(route: APIRoute) -> bool:
    return route.body_field is not None and isinstance(route.body_field.field_info, params.Form)


def _json(value: object) -> bytes:
    """JSON as a client writes it: a surrogate as \\ud800 (httpx's json= can't encode one)."""
    return json.dumps(value).encode()


# ---------------------------------------------------------------------------
# The URL: before sign-in, every route
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_text_that_isnt_text_in_a_query_parameter_is_a_400_on_every_route() -> None:
    client = TestClient(app, raise_server_exceptions=False)
    for method, path, route in _routes():
        query = {name: NUL for name in _query_names(route.dependant) | {"also"}}
        for headers in ({}, {"Authorization": "Bearer lv_nobodys"}):
            r = client.request(method, _filled(path), params=query, headers=headers)
            _refused(r, "query")
            assert r.headers["X-Frame-Options"] == SECURITY_HEADERS["X-Frame-Options"], (method, path)
        # One parameter alone, by any name: its name too.
        _refused(client.request(method, _filled(path), params={"also": NUL}), "query")
        _refused(client.request(method, f"{_filled(path)}?a%00b=1"), "query")
        for raw in NOT_UTF8:
            _refused(client.request(method, f"{_filled(path)}?also={raw}"), "query", "not_utf8")


@pytest.mark.fast
def test_text_that_isnt_text_in_the_path_is_a_400_on_every_route() -> None:
    client = TestClient(app, raise_server_exceptions=False)
    with_parameters = 0
    for method, path, _ in _routes():
        if "{" not in path:
            continue
        with_parameters += 1
        for headers in ({}, {"Authorization": "Bearer lv_nobodys"}):
            _refused(client.request(method, _filled(path, "a%00b"), headers=headers), "path")
        for raw in NOT_UTF8:
            _refused(client.request(method, _filled(path, raw)), "path", "not_utf8")
    assert with_parameters > 100, with_parameters
    _refused(client.get("/v1/no-such-route/a%00b"), "path")
    _refused(client.get("/v1/no-such-route/%FF"), "path", "not_utf8")


@pytest.mark.fast
def test_the_public_pages_lookups_never_see_one() -> None:
    """Signed out, the middleware looks a public library or project up by an
    id the URL gives (the control database): one there is the gate's 400,
    before it, as anywhere else in the URL."""
    client = TestClient(app, raise_server_exceptions=False)
    for path, where, found in (
        ("/v1/public/projects/a%00b", "path", "nul"),
        ("/v1/public/projects/a%00b/assets", "path", "nul"),
        ("/v1/public/projects/a%ED%A0%80b", "path", "not_utf8"),
        ("/v1/libraries/lib%00/directories", "path", "nul"),
        ("/v1/libraries/lib%FF/directories", "path", "not_utf8"),
        ("/v1/query?f=library:lib_%00", "query", "nul"),
        ("/v1/query?f=library:lib_%ED%A0%80", "query", "not_utf8"),
        ("/v1/similar?library_id=a%00b&asset_id=x", "query", "nul"),
        ("/v1/assets/x/artifacts/lumiverb.preview?public_project_id=a%00b", "query", "nul"),
        ("/v1/assets/x?public_library_id=%FF", "query", "not_utf8"),
    ):
        _refused(client.get(path), where, found)


@pytest.mark.fast
def test_a_route_a_server_doesnt_expose_is_a_404_whatever_it_holds(monkeypatch) -> None:
    """The exposure gate stays outermost (exposure.py): from elsewhere, a route
    it doesn't list is a 404 before the URL is looked at; one it lists, a 400."""
    monkeypatch.setenv("LUMIVERB_EXPOSURE", "demo")
    get_settings.cache_clear()
    try:
        visitor = TestClient(app, raise_server_exceptions=False, headers={"X-Forwarded-For": "203.0.113.9"})
        for path in ("/v1/ai?q=a%00b", "/v1/producers?q=%FF", "/v1/libraries/a%00b/reader-work"):
            r = visitor.get(path)
            assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found"), (path, r.text)
        _refused(visitor.get("/v1/query?q=a%00b"), "query")
        _refused(visitor.get("/v1/query?q=%FF"), "query", "not_utf8")
        _refused(visitor.get("/v1/assets/a%00b"), "path")
    finally:
        monkeypatch.delenv("LUMIVERB_EXPOSURE")
        get_settings.cache_clear()


@pytest.mark.fast
@pytest.mark.parametrize(("path", "raw_path", "query", "refused"), [
    ("/v1/x", b"/v1/x", b"", None), ("/v1/x", b"/v1/x", b"q=%2500", None),
    ("/v1/x", b"/v1/x", b"q=a+b&r=%E2%9C%93&s=%F0%9F%98%80", None),
    ("/v1/café", b"/v1/caf%C3%A9", b"", None),
    ("/v1/x", b"/v1/x", b"q=%00", ("query", "nul")), ("/v1/x", b"/v1/x", b"%00=q", ("query", "nul")),
    ("/v1/x", b"/v1/x", b"q=1&r=a%00", ("query", "nul")), ("/v1/x", b"/v1/x", b"q=a\x00b", ("query", "nul")),
    ("/v1/x", b"/v1/x", b"q=%ED%A0%80", ("query", "not_utf8")), ("/v1/x", b"/v1/x", b"q=%ff", ("query", "not_utf8")),
    ("/v1/x", b"/v1/x", b"q=\xe9", ("query", "not_utf8")),
    ("/v1/a\x00b", b"/v1/a%00b", b"q=%00", ("path", "nul")),
    ("/v1/�", b"/v1/%FF", b"", ("path", "not_utf8")),
    ("/v1/a\ud800", None, b"", ("path", "surrogate")),  # a server that gives no raw_path: the path as matched
])
def test_where_the_url_holds_one(path, raw_path, query, refused) -> None:
    scope = {"type": "http", "path": path, "query_string": query, **({"raw_path": raw_path} if raw_path else {})}
    assert no_nul.in_url(scope) == refused


@pytest.mark.fast
@pytest.mark.parametrize(("value", "found"), [
    ({"a": "b", "c": [1, 2.5, None, True, {"d": "e"}], "f": "café \U0001f600"}, None),
    ({"a\x00": 1}, "nul"), ({"a": [[{"b": ["c", "d\x00"]}]]}, "nul"), ("\x00", "nul"),
    ({"a": "\ud800"}, "surrogate"), ({"\udc00": 1}, "surrogate"), ({"a": [{"b": ["x\udfffy"]}]}, "surrogate"),
    ({"exif": '{"Make": "Can\\u0000on"}'}, "nul"),  # JSON text a route decodes: as it decodes it
    ({"exif": '{"Make": "Can\\uD800on"}'}, "surrogate"),
    ({"exif": '{"Make": "\\ud83d\\ude00"}'}, None),  # a whole character, written as its pair
    ({"people": ' [{"seen": ["\\u0000"]}]'}, "nul"),
    ({"name": "a\\u0000b"}, None),  # a backslash and five letters: text, as it reads
    ({"note": '"\\u0000"'}, None), ({"note": "{not json \\ud800"}, None),  # only an object or array is JSON here
    ({"exif": '{"Make": "Can\\\\u0000on"}'}, None),
    ({"deep": json.loads("[" * 5000 + "]" * 5000)}, None),
])
def test_a_json_value_holds_one_anywhere_it_has_text(value, found) -> None:
    assert no_nul._found(value) == found


# ---------------------------------------------------------------------------
# A body: after sign-in, every route
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_json_body_holding_one_is_a_400_on_every_route(env) -> None:
    json_routes = 0
    headers = {**env.admin, "Content-Type": "application/json"}
    for method, path, route in _routes():
        for held, found in ((NUL, "nul"), (HALF, "surrogate")):
            r = env.client.request(method, _filled(path), content=_json({"name": held, "items": [{"id": "x"}]}),
                                   headers=headers)
            _refused(r, "body", found)
        json_routes += route.body_field is not None and not _takes_a_form(route)
    assert json_routes > 100, json_routes


@pytest.mark.slow
def test_every_way_json_holds_one(env) -> None:
    def post(content: bytes, content_type: str = "application/json"):
        return env.client.post("/v1/projects", content=content, headers={**env.admin, "Content-Type": content_type})

    for held, found in ((NUL, "nul"), (HALF, "surrogate"), ("\udc00", "surrogate"), ("x\udfff", "surrogate")):
        for body in ({"name": "x", "description": held}, {"name": "x", held: 1}, {"name": "x", "more": [[{"y": ["z", held]}]]}):
            _refused(post(_json(body)), "body", found)
        _refused(post(_json({"name": held}), "application/json; charset=utf-8"), "body", found)
        _refused(post(_json({"name": held}), "application/merge-patch+json"), "body", found)
        _refused(post(json.dumps({"name": held}).encode("utf-16")), "body", found)  # as Python reads JSON: UTF-16 too
    _refused(post(_json({"name": '{"a": "\\u0000"}'})), "body")
    _refused(post(_json({"name": '{"a": "\\uDBFF"}'})), "body", "surrogate")
    _refused(post(b'{"name": "\\uD800"}'), "body", "surrogate")  # its hex as any case
    _refused(post(b'{"name": "a\xed\xa0\x80b"}'), "body", "surrogate")  # a surrogate's own bytes: Python reads them

    # Text that only looks like one: a backslash and five letters, kept as written; a whole character, written
    # as its pair, is the character.
    for named in (f"a\\u0000b {uuid.uuid4().hex[:6]}", f"\\ud83d\\ude00 {uuid.uuid4().hex[:6]}"):
        r = post(_json({"name": named}))
        assert r.status_code in (200, 201) and r.json()["name"] == named, r.text
    smiling = f"\U0001f600 {uuid.uuid4().hex[:6]}"
    r = post(_json({"name": smiling}))  # written 😀
    assert r.status_code in (200, 201) and r.json()["name"] == smiling, r.text
    assert post(b'{"name": "a\x00b"}').status_code == 422, "a NUL itself is no JSON: refused as ever"
    assert post(b'{"name": ').status_code == 422


@pytest.mark.slow
def test_a_forms_text_fields_holding_one_are_a_400_and_bytes_are_never_read(env) -> None:
    library_id = _library(env)
    form_routes = [(method, path) for method, path, route in _routes() if _takes_a_form(route)]
    assert ("POST", "/v1/ingest") in form_routes and len(form_routes) >= 4, form_routes
    for method, path in form_routes:
        fields = {"library_id": library_id, "rel_path": "a.jpg", "lineage": "{}", "note": NUL}
        r = env.client.request(method, _filled(path), data=fields, headers=env.admin,
                               files={"file": ("p.jpg", JPEG_WITH_NULS, "image/jpeg")})
        _refused(r, "form")
        _refused(env.client.request(method, _filled(path), data=fields, headers=env.admin), "form")  # urlencoded
        _refused(env.client.request(method, _filled(path), data={**fields, "note": "x", NUL: "y"},
                                    headers=env.admin), "form")  # a field's name
    # JSON a route decodes from a field (ingest's exif): as it decodes it.
    for held, found in ((NUL, "nul"), (HALF, "surrogate")):
        r = env.client.post("/v1/ingest", headers=env.admin,
                            files={"lumiverb.preview": ("p.jpg", JPEG_WITH_NULS, "image/jpeg")},
                            data={"library_id": library_id, "rel_path": "b.jpg", "file_size": "1",
                                  "exif": json.dumps({"Make": held})})
        _refused(r, "form", found)

    # A file in a form and an upload's chunks are bytes: their NUL bytes are theirs. A preview and a video's
    # (a JPEG's and an MP4's NUL bytes, through ingest's form and an artifact's), then an analysis proxy in chunks.
    clip = _ingest(env, library_id, File("n/clip.mov", "nul bytes"))
    data = playable(2.0)
    assert b"\x00" in data
    upload = env.client.post(f"/v1/assets/{clip}/uploads", headers=env.admin, json={
        "artifact": "lumiverb.video.analysis_proxy", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "lineage": made("lumiverb.video.analysis_proxy")})
    assert upload.status_code == 200, upload.text
    chunk_url = upload.json()["chunk_url"]
    r = env.client.put(chunk_url, params={"offset": 0}, content=data,
                       headers={**env.admin, "Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    assert env.client.post(f"{chunk_url}/finish", headers=env.admin).status_code == 200


@pytest.mark.slow
def test_a_body_is_read_only_for_a_request_sign_in_lets_through(env) -> None:
    body = {"name": NUL}
    r = env.client.post("/v1/projects", json=body, headers={"Authorization": ""})
    assert (r.status_code, r.json()["error"]["code"]) == (401, "unauthorized"), r.text
    r = env.client.post("/v1/projects", json=body, headers={"Authorization": "Bearer lv_nobodys"})
    assert r.status_code == 401, r.text
    # What takes no sign-in still reads its body: signing in, setup, upkeep.
    _refused(env.client.post("/v1/auth/login", json={"email": NUL, "password": "x"}, headers={"Authorization": ""}),
             "body")
    _refused(env.client.post("/v1/auth/login", content=_json({"email": HALF, "password": "x"}),
                             headers={"Authorization": "", "Content-Type": "application/json"}), "body", "surrogate")


@pytest.mark.slow
def test_a_public_pages_search_holding_one_is_a_400(env) -> None:
    library_id = _library(env)
    make_clip(env.tenant_url, library_id, "pub/one.jpg")
    assert env.client.patch(f"/v1/libraries/{library_id}", json={"is_public": True},
                            headers=env.admin).status_code == 200
    anyone = {"Authorization": ""}
    seen = env.client.get("/v1/query", params={"f": f"library:{library_id}"}, headers=anyone)
    assert seen.status_code == 200 and len(seen.json()["items"]) == 1, seen.text
    for params_ in ({"f": [f"library:{library_id}", f"query:{NUL}"]}, {"f": f"library:{library_id}", "after": NUL},
                    {"f": f"library:{library_id}", "sort": NUL}):
        _refused(env.client.get("/v1/query", params=params_, headers=anyone), "query")
    _refused(env.client.get(f"/v1/query?f=library:{library_id}&f=query:%ED%A0%80", headers=anyone), "query", "not_utf8")
    _refused(env.client.get(f"/v1/libraries/{library_id}/directories", params={"path": NUL}, headers=anyone), "query")


# ---------------------------------------------------------------------------
# One that slips past: the database's refusal, the same 400
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_one_the_database_refuses_is_the_same_400(env) -> None:
    """The batch rating reads its body whatever it's called: sent as text, the
    gate doesn't read it, and the clip it names is looked up as it came."""
    library_id = _library(env)
    clip = make_clip(env.tenant_url, library_id, "r/one.jpg")
    for held, found in ((NUL, "nul"), (HALF, "surrogate")):
        body = _json({"asset_ids": [clip, held], "favorite": True})
        r = env.client.put("/v1/assets/ratings", content=body, headers={**env.admin, "Content-Type": "text/plain"})
        _refused(r, None, found)
        assert r.headers["X-Frame-Options"] == SECURITY_HEADERS["X-Frame-Options"]
    clean = _json({"asset_ids": [clip], "favorite": True})
    r = env.client.put("/v1/assets/ratings", content=clean, headers={**env.admin, "Content-Type": "text/plain"})
    assert r.status_code == 200, r.text


def _handler(exc: Exception):
    """The app's handler for an exception, as Starlette looks it up: by its class, then its bases."""
    return next(app.exception_handlers[cls] for cls in type(exc).__mro__ if cls in app.exception_handlers)


@pytest.mark.slow
def test_the_database_refusals_it_answers_and_no_others(env) -> None:
    db = create_engine(env.tenant_url)
    refused: list[tuple[Exception, str]] = []
    with db.connect() as conn:
        for statement, value, found in (("SELECT :v", NUL, "nul"), ("SELECT CAST(:v AS jsonb)", '{"a": "\\u0000"}', "nul"),
                                        ("SELECT :v", HALF, "surrogate"),
                                        ("SELECT CAST(:v AS jsonb)", '{"a": "\\ud800"}', "surrogate")):
            try:
                conn.execute(text(statement), {"v": value})
            except Exception as exc:  # what the database layer raises, as a route meets it
                refused.append((exc, found))
            conn.rollback()
        try:
            conn.execute(text("SELECT CAST('x' AS integer)"))
        except DataError as exc:
            other_data_error = exc
    db.dispose()
    assert [type(exc) for exc, _ in refused] == [ValueError, DataError, UnicodeEncodeError, DataError], refused

    request = Request({"type": "http", "method": "GET", "path": "/v1/x", "headers": [], "query_string": b""})
    for exc, found in refused:
        response = asyncio.run(_handler(exc)(request, exc))
        assert response.status_code == 400, found
        assert json.loads(response.body)["error"] == {"code": "not_text", "message": NOT_TEXT_SAYS[found],
                                                      "details": {"found": found}}
    try:
        HALF.encode("utf-8")  # one a response would meet, never the database: the server's, a 500
    except UnicodeEncodeError as exc:
        not_the_databases = exc
    for exc in (ValueError("something else"), other_data_error, not_the_databases):
        with pytest.raises(type(exc)):
            asyncio.run(_handler(exc)(request, exc))
