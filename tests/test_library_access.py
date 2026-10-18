# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A person's role on the library a request is about (ADR-018 decision 8,
phase 8; src/server/api/library_access.py).

Against the real API and databases (tests/test_scans_api.py's accounts): a
person who edits library A, only views B and can't see C. Whatever a route
names by its path, in C, is a 404 on every route, as one that isn't there;
in B, a route that edits is a 403; in A it isn't. What a route names in its
body, form or query is marked and walked the same way, and a route that
names one and isn't marked fails the walk. An account admin is every
library's admin; a key a person made is never above its own role.
"""

from __future__ import annotations

import re
import typing
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import text
from sqlmodel import Session

from src.server.api import library_access
from src.server.api.dependencies import require_editor, require_reader, require_tenant_admin
from src.server.api.main import app
from src.server.api.middleware import _skip_tenant_middleware
from src.server.config import get_settings
from src.server.database import get_control_session
from src.server.repository.control_plane import ApiKeyRepository, UserRepository
from src.server.repository.library_roles import LibraryRoleRepository
from tests.test_scans_api import File, _library, _ok, _scan, env  # noqa: F401 — the shared fixture
from tests.videos import playable

NOW = datetime(2026, 10, 17, tzinfo=UTC)
PASSWORD = "a long enough password"

# Path parameters that name something of the account's, not of one library.
ACCOUNT = {"artifact", "artifact_type", "default_id", "device_id", "filter_id", "job", "key_id", "machine_id",
           "target", "tenant_id", "token", "user_id", "view_id"}
# What spans libraries, shown only where a person can see it once those parts come (ADR-018 decision 8):
# a project's clips, a person's faces, a cluster's.
SPANNING = {"project_id", "person_id", "cluster_id"}
# Body, form and query fields naming what spans libraries, for the same parts.
SPANNING_FIELDS = {
    ("POST", "/v1/people/{person_id}/merge"), ("POST", "/v1/faces/clusters/{cluster_id}/name"),
    ("POST", "/v1/faces/{face_id}/assign"), ("POST", "/v1/projects/empty-trash"), ("GET", "/v1/producers"),
}
NAMING = {"library_id", "library_ids", "asset_id", "asset_ids", "same_as", "face_ids", "items", "person_id",
          "source_person_id", "project_id", "project_ids"}


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _tenant_of(env) -> str:
    with get_control_session() as session:
        return ApiKeyRepository(session).get_by_plaintext(env.admin["Authorization"][7:]).tenant_id


def _person(env, email: str, role: str, *, every_library: bool = True) -> str:
    r = env.client.post("/v1/users", headers=env.admin, json={"email": email, "password": PASSWORD, "role": role})
    assert r.status_code == 201, r.text
    user_id = r.json()["user_id"]
    if not every_library:
        with get_control_session() as session:
            user = UserRepository(session).get_by_id(user_id)
            user.every_library = False
            session.add(user)
            session.commit()
    return user_id


def _key(env, user_id: str, role: str) -> dict:
    """A key the person made, of this role."""
    with get_control_session() as session:
        _, plaintext = ApiKeyRepository(session).create(_tenant_of(env), "theirs", role=role,
                                                        created_by_user_id=user_id)
    return _bearer(plaintext)


def _set(env, library_id: str, user_id: str, role: str | None) -> None:
    with Session(env.db) as session:
        LibraryRoleRepository(session).set(library_id, user_id, role, by=None, now=NOW)
        session.commit()


def _things(env, library_id: str) -> dict[str, str]:
    """One of each thing a route names by its path, in this library."""
    clip = _scan(env, library_id, [File("v.mov", f"clip of {library_id}")], headers=env.admin).uploaded["v.mov"]
    scan_id = _ok(env.client.post(f"/v1/libraries/{library_id}/scans", json={}, headers=env.admin))["scan_id"]
    data = playable()
    import hashlib

    from tests.machine_lineage import made

    upload = _ok(env.client.post(f"/v1/assets/{clip}/uploads", headers=env.admin, json={
        "artifact": "video_preview", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "lineage": made("video_preview")}))
    ids = {"library_id": library_id, "asset_id": clip, "scan_id": scan_id, "upload_id": upload["upload_id"],
           "chunk_id": f"chk_{library_id}", "scene_id": f"scn_{library_id}", "face_id": f"fac_{library_id}"}
    with env.db.begin() as conn:
        conn.execute(text("INSERT INTO video_index_chunks (chunk_id, asset_id, chunk_index, start_ms, end_ms,"
                          " created_at) VALUES (:c, :a, 0, 0, 30000, now())"), {"c": ids["chunk_id"], "a": clip})
        conn.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                          " created_at) VALUES (:s, :a, 0, 0, 30000, 0, now())"), {"s": ids["scene_id"], "a": clip})
        conn.execute(text("INSERT INTO faces (face_id, asset_id, created_at) VALUES (:f, :a, now())"),
                     {"f": ids["face_id"], "a": clip})
    return ids


@pytest.fixture(scope="module")
def people(env):
    """An account editor who edits A (their account role), only views B and
    can't see C, with a key they made; A's, B's and C's things."""
    a, b, c = _library(env), _library(env), _library(env)
    user_id = _person(env, "three-libraries@example.com", "editor")
    _set(env, b, user_id, "viewer")
    _set(env, c, user_id, "none")
    return SimpleNamespace(user_id=user_id, key=_key(env, user_id, "editor"),
                           a=_things(env, a), b=_things(env, b), c=_things(env, c))


def _routes():
    """Every route the middleware takes an account's key on: (method, path, route)."""
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute) or _skip_tenant_middleware(context.path):
            continue
        for method in sorted(set(context.methods or ()) - {"HEAD", "OPTIONS"}):
            yield method, context.path, route


def _needs(route: APIRoute) -> set:
    found = set()

    def walk(dependant):
        for dep in dependant.dependencies:
            if dep.call in (require_editor, require_tenant_admin, require_reader):
                found.add(dep.call)
            walk(dep)
    walk(route.dependant)
    return found


def _url(path: str, things: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)(:\w+)?\}", lambda m: things.get(m.group(1), "proxy" if m.group(1) == "artifact_type"
                                                            else "nope"), path)


@pytest.mark.slow
def test_every_path_parameter_names_one_library_or_none():
    """A new kind of path parameter is placed here first: in one library
    (the gate looks it up), the account's, or spanning libraries."""
    params = {p for _, path, _ in _routes() for p in re.findall(r"\{(\w+)", path)}
    assert params <= set(library_access.PATH) | ACCOUNT | SPANNING, params - set(library_access.PATH) - ACCOUNT
    assert set(library_access.PATH.values()) <= set(library_access._NOT_FOUND)


@pytest.mark.slow
def test_a_library_they_cant_see_doesnt_exist_on_any_route(env, people):
    walked = 0
    for method, path, _ in _routes():
        if not set(re.findall(r"\{(\w+)", path)) & set(library_access.PATH):
            continue
        r = env.client.request(method, _url(path, people.c), headers=people.key)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
        walked += 1
    assert walked > 60


@pytest.mark.slow
def test_editing_needs_editor_on_the_library_it_is_in(env, people):
    edited = 0
    for method, path, route in _routes():
        if not set(re.findall(r"\{(\w+)", path)) & set(library_access.PATH):
            continue
        needs = _needs(route)
        if require_editor not in needs or needs & {require_tenant_admin, require_reader}:
            continue
        viewer = env.client.request(method, _url(path, people.b), headers=people.key)
        assert viewer.status_code == 403, (method, path, viewer.status_code, viewer.text)
        editor = env.client.request(method, _url(path, people.a), headers=people.key)
        assert editor.status_code != 403, (method, path, editor.text)
        edited += 1
    assert edited > 30


def _body(fields, things: dict[str, str]) -> dict:
    """A request naming these things where a route's marks say."""
    json_body, form, query = {}, {}, {}
    for (where, name, sub, kind), _ in fields:
        id_ = things[{"library": "library_id", "asset": "asset_id", "face": "face_id"}[kind]]
        value = [{sub: id_}] if sub else ([id_] if name.endswith("s") else id_)
        {"body": json_body, "form": form, "query": query}[where][name] = value
    out = {"params": query}
    if form:
        out.update(data=form, files={"proxy": ("p.jpg", b"jpeg", "image/jpeg")})
    elif json_body:
        out["json"] = json_body
    return out


def _model_fields(annotation) -> set[str]:
    """A body's fields, its model's (an optional one's or a union's too)."""
    fields = getattr(annotation, "model_fields", None)
    if fields is not None:
        return set(fields)
    return {n for arg in typing.get_args(annotation) for n in _model_fields(arg)}


def _fields_named(route: APIRoute) -> set[str]:
    """What a route's body, form and query name, by their fields' names."""
    found = set()

    def walk(dependant):
        for q in dependant.query_params:
            if q.name in NAMING:
                found.add(q.name)
        for b in dependant.body_params:
            fields = _model_fields(b.field_info.annotation)
            if fields:
                found.update(n for n in fields if n in NAMING)
            elif b.name in NAMING:
                found.add(b.name)
        for dep in dependant.dependencies:
            walk(dep)
    walk(route.dependant)
    return found


@pytest.mark.slow
def test_what_a_route_names_in_its_body_or_query_is_marked_and_checked(env, people):
    """Every route naming a library, clip or face in its body, form or
    query says so (names), or spans libraries (SPANNING_FIELDS); each
    marked one, sent C's, is a 404."""
    unmarked = []
    for method, path, route in _routes():
        fields = library_access.marked(route.endpoint)
        named = _fields_named(route) - set(re.findall(r"\{(\w+)", path))
        if fields is None:
            if named and (method, path) not in SPANNING_FIELDS:
                unmarked.append((method, path, sorted(named)))
            continue
        assert {name for (_, name, _, _), _ in fields} <= named | {"asset_ids"}, (method, path)
        url = _url(path, people.a)  # the path's own things where they can be seen
        r = env.client.request(method, url, headers=people.key, **_body(fields, people.c))
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    assert not unmarked, unmarked


@pytest.mark.slow
def test_an_account_viewer_edits_the_library_theyre_its_editor_of(env, monkeypatch):
    """Signed in: a role set above their account role is theirs there."""
    monkeypatch.setattr(get_settings(), "jwt_secret", "a-test-secret-long-enough-for-hs256-signing")
    a, b = _library(env), _library(env)
    clip_a = _scan(env, a, [File("a.jpg", "a")], headers=env.admin).uploaded["a.jpg"]
    clip_b = _scan(env, b, [File("b.jpg", "b")], headers=env.admin).uploaded["b.jpg"]
    user_id = _person(env, "viewer-editor@example.com", "viewer")
    _set(env, a, user_id, "editor")
    token = _ok(env.client.post("/v1/auth/login", json={"email": "viewer-editor@example.com",
                                                         "password": PASSWORD}))["access_token"]
    signed_in = _bearer(token)
    assert env.client.put(f"/v1/assets/{clip_a}/note", json={"text": "mine to edit"},
                          headers=signed_in).status_code == 200
    assert env.client.put(f"/v1/assets/{clip_b}/note", json={"text": "no"}, headers=signed_in).status_code == 403
    assert env.client.get(f"/v1/assets/{clip_b}", headers=signed_in).status_code == 200, "they view the rest"
    # A key they make is a viewer's (no higher than their account role): it edits nothing.
    viewer_key = _key(env, user_id, "viewer")
    assert env.client.put(f"/v1/assets/{clip_a}/note", json={"text": "x"}, headers=viewer_key).status_code == 403


@pytest.mark.slow
def test_private_libraries_and_people_who_see_only_whats_given(env):
    open_, private = _library(env), _library(env)
    with env.db.begin() as conn:
        conn.execute(text("UPDATE libraries SET private = true WHERE library_id = :l"), {"l": private})
    clip_open = _scan(env, open_, [File("o.jpg", "o")], headers=env.admin).uploaded["o.jpg"]
    clip_private = _scan(env, private, [File("p.jpg", "p")], headers=env.admin).uploaded["p.jpg"]
    member = _key(env, _person(env, "member@example.com", "editor"), "editor")
    assert env.client.get(f"/v1/assets/{clip_open}", headers=member).status_code == 200
    assert env.client.get(f"/v1/assets/{clip_private}", headers=member).status_code == 404
    assert env.client.get(f"/v1/assets/{clip_private}", headers=env.admin).status_code == 200, "an account admin's"

    guest_id = _person(env, "guest@example.com", "editor", every_library=False)
    guest = _key(env, guest_id, "editor")
    assert env.client.get(f"/v1/assets/{clip_open}", headers=guest).status_code == 404
    _set(env, private, guest_id, "viewer")
    assert env.client.get(f"/v1/assets/{clip_private}", headers=guest).status_code == 200
    assert env.client.put(f"/v1/assets/{clip_private}/note", json={"text": "x"}, headers=guest).status_code == 403


@pytest.mark.slow
def test_an_account_admin_is_every_librarys_admin(env):
    library_id = _library(env)
    clip = _scan(env, library_id, [File("x.jpg", "x")], headers=env.admin).uploaded["x.jpg"]
    admin_id = _person(env, "second-admin@example.com", "admin")
    _set(env, library_id, admin_id, "none")  # set or not, an account admin sees it
    admin = _key(env, admin_id, "admin")
    assert env.client.put(f"/v1/assets/{clip}/note", json={"text": "an admin's"}, headers=admin).status_code == 200
    assert env.client.put(f"/v1/assets/{clip}/note", json={"text": "the scheduler's"},
                          headers=env.admin).status_code == 200, "a key no person made: its own role everywhere"


@pytest.mark.slow
def test_a_key_whose_maker_is_gone_does_nothing(env):
    user_id = _person(env, "gone@example.com", "editor")
    key = _key(env, user_id, "editor")
    with get_control_session() as session:
        session.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": user_id})  # keys left behind
        session.commit()
    assert env.client.get("/v1/libraries", headers=key).status_code == 401
