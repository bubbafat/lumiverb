# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What spans libraries shows a person only what they can see (ADR-018
decision 8, phase 8; src/server/api/library_access.py seen, in_sight).

Against the real API and databases (tests/test_scans_api.py's accounts): a
library the person can't see holds clips with words, a camera, a failure,
a clip in the trash and one archived, the person's own favorite and a
reported change. Every list and search the person asks for (every GET with
nothing in its path, and searches by its words) says nothing of it: not its
id, name, clips, paths or camera. Counts are counted over what they see.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlmodel import Session

from src.server.database import get_control_session
from src.server.repository.control_plane import PublicProjectRepository
from src.server.repository.people import PersonRepository
from tests.test_library_access import _key, _person, _routes, _set, _tenant_of
from tests.test_scans_api import File, _library, _ok, _scan, env  # noqa: F401 — the shared fixture

# Lists the walk must reach: those that span libraries the most.
SPANNING = {"/v1/people", "/v1/people/dismissed", "/v1/faces/clusters", "/v1/projects"}


@pytest.fixture(scope="module")
def hidden(env):
    """Library A the person edits; library C they can't see, and all it holds."""
    a, c = _library(env), _library(env)
    word = "zq" + uuid.uuid4().hex[:8]  # one word, in no other clip
    clips_a = _scan(env, a, [File("seen/a1.jpg", "a1"), File("seen/a2.jpg", "a2"), File("seen/a3.jpg", "a3")],
                    headers=env.admin).uploaded
    clips_c = _scan(env, c, [File(f"{word}/c1.jpg", "c1"), File(f"{word}/c2.jpg", "c2"),
                             File(f"{word}/c3.jpg", "c3")], headers=env.admin).uploaded
    c1, c2, c3 = (clips_c[f"{word}/c{i}.jpg"] for i in (1, 2, 3))
    a1, a2, a3 = (clips_a[f"seen/a{i}.jpg"] for i in (1, 2, 3))
    for clip in (a1, c1):
        _ok(env.client.put(f"/v1/assets/{clip}/note", json={"text": f"{word} seen at the shoot"}, headers=env.admin))
    with env.db.begin() as conn:
        conn.execute(text("UPDATE assets SET camera_make = :m WHERE asset_id = :a"),
                     {"m": f"Cam{word}", "a": c1})
    _ok(env.client.post("/v1/producers/failures", headers=env.admin, json={
        "items": [{"asset_id": c1, "artifact": "lumiverb.image.likeness", "error": f"failed {word}"},
                  {"asset_id": a1, "artifact": "lumiverb.image.likeness", "error": "failed here"}]}))
    assert env.client.delete(f"/v1/assets/{c2}", headers=env.admin).status_code in (200, 204)
    _ok(env.client.post("/v1/assets/archive", json={"asset_ids": [c3]}, headers=env.admin))
    library_c = _ok(env.client.get(f"/v1/libraries/{c}", headers=env.admin))
    _ok(env.client.post("/v1/changes", headers=env.admin,
                        json={"paths": [library_c["root_path"] + f"/{word}/new.jpg"]}))

    user_id = _person(env, f"sees-a-{word}@example.com", "editor")
    _set(env, c, user_id, "none")
    key, key_id = _key(env, user_id, "editor", with_id=True)
    with env.db.begin() as conn:  # their favorite, from before they lost it
        conn.execute(text("INSERT INTO asset_ratings (asset_id, user_id, favorite, stars, updated_at)"
                          " VALUES (:a, :u, true, 0, now())"), {"a": c1, "u": f"key:{key_id}"})

    # Projects: one of C's clips alone, one of both libraries (shared), and the person's own with one of C's
    # clips in it from before they lost it, published.
    only_c = _ok(env.client.post("/v1/projects", headers=env.admin, json={
        "name": f"only {word}", "visibility": "shared", "asset_ids": [c1]}), 201)["project_id"]
    mixed = _ok(env.client.post("/v1/projects", headers=env.admin, json={
        "name": "both libraries", "visibility": "shared", "asset_ids": [a1, c1, a3]}), 201)["project_id"]
    theirs = _ok(env.client.post("/v1/projects", headers=key, json={
        "name": "their own", "visibility": "private", "asset_ids": [a1]}), 201)["project_id"]
    with env.db.begin() as conn:
        conn.execute(text("INSERT INTO project_assets (project_id, asset_id, position, added_at)"
                          " VALUES (:p, :c1, 1, now()), (:p, :a2, 2, now()), (:m, :c2, 3, now())"),
                     {"p": theirs, "c1": c1, "a2": a2, "m": mixed, "c2": c2})
        conn.execute(text("UPDATE projects SET cover_asset_id = :c1 WHERE project_id = :m"), {"c1": c1, "m": mixed})
        conn.execute(text("UPDATE projects SET visibility = 'public' WHERE project_id = :p"), {"p": theirs})
    with get_control_session() as session:
        PublicProjectRepository(session).upsert(theirs, _tenant_of(env))
    assert env.client.delete(f"/v1/assets/{a3}", params={"remove_from_projects": "true"},
                             headers=env.admin).status_code in (200, 204)

    # Faces: a person seen only in C, one seen in both (shown by C's face), a dismissed one only in C, and
    # unnamed faces grouped: one group of both libraries' faces, one of C's alone.
    face = {name: f"fac_{name}_{uuid.uuid4().hex[:12]}" for name in ("a", "c", "a2", "c2", "c3", "c4", "c5")}
    with env.db.begin() as conn:
        for name, clip in (("a", a1), ("c", c1), ("a2", a1), ("c2", c1), ("c3", c1), ("c4", c1), ("c5", c1)):
            conn.execute(text("INSERT INTO faces (face_id, asset_id, detection_confidence, created_at)"
                              " VALUES (:f, :a, :conf, now())"),
                         {"f": face[name], "a": clip, "conf": 0.99 if name == "c" else 0.5})
    with Session(env.db) as session:
        people = PersonRepository(session)
        only_c_person = people.create(f"Person {word}", face_ids=[face["c"]]).person_id
        both = people.create("Seen in both", face_ids=[face["a"], face["c5"]]).person_id
        dismissed = people.create_dismissed(face_ids=[face["c4"]]).person_id
        session.execute(text("UPDATE people SET representative_face_id = :c WHERE person_id = :p"),
                        {"c": face["c5"], "p": both})
        session.commit()
        from src.server.api.routers.people import cluster_id_of
        from src.server.repository.faces import face_clusters_version
        from src.server.repository.system_metadata import SystemMetadataRepository

        groups = [[face["a2"], face["c2"]], [face["c3"]]]
        SystemMetadataRepository(session).set_value("face_clusters_cache", json.dumps({
            "clusters": [{"cluster_index": i, "size": len(ids), "face_ids": ids, "newest": None,
                          "faces": [{"face_id": f, "asset_id": a1 if f == face["a2"] else c1} for f in ids]}
                         for i, ids in enumerate(groups)],
            "truncated": False, "computed_at": "2026-10-18T00:00:00+00:00",
            "version": face_clusters_version(session)}))
        session.commit()
    return SimpleNamespace(a=a, c=c, word=word, a1=a1, a2=a2, a3=a3, c_clips=(c1, c2, c3), key=key,
                           only_c=only_c, mixed=mixed, theirs=theirs, face=face, only_c_person=only_c_person,
                           both=both, dismissed=dismissed, both_group=cluster_id_of(groups[0]),
                           c_group=cluster_id_of(groups[1]),
                           marks=(c, library_c["name"], word, c1, c2, c3, only_c, only_c_person, dismissed,
                                  face["c"], face["c2"], face["c3"], face["c4"], face["c5"], cluster_id_of(groups[1])))


def _says_nothing_of(r, marks, *, theirs: str = "") -> None:
    """Nothing in the answer names the library they can't see. theirs: words of what they can see, left out first
    (their own clip's note, its search snippet: the same words as C's, whose clip would still show by its id)."""
    body = r.text.replace(theirs, "") if theirs else r.text
    found = [m for m in marks if m in body]
    assert not found, (r.request.method, str(r.request.url), found, body[:500])


@pytest.mark.slow
def test_no_list_says_anything_of_a_library_they_cant_see(env, hidden):
    walked = 0
    assert {path for _, path, _ in _routes()} >= SPANNING
    reached = set()
    for method, path, _ in _routes():
        if method != "GET" or "{" in path:
            continue
        r = env.client.get(path, headers=hidden.key)
        if r.status_code >= 400:
            continue  # nothing said (an admin's route, or one that needs more)
        _says_nothing_of(r, hidden.marks)
        walked += 1
        reached.add(path)
    assert walked > 15 and reached >= SPANNING
    for params in ({"include_trashed": "true"},):
        _says_nothing_of(env.client.get("/v1/libraries", params=params, headers=hidden.key), hidden.marks)


@pytest.mark.slow
def test_search_finds_only_what_they_can_see(env, hidden):
    for path in ("/v1/query", "/v1/assets/facets"):
        for f in ([f"query:{hidden.word}"], [], [f"library:{hidden.c}"], [f"library:{hidden.a}"],
                  [f"camera_make:Cam{hidden.word}"]):
            r = env.client.get(path, params={"f": f}, headers=hidden.key)
            assert r.status_code == 200, (path, f, r.text)
            _says_nothing_of(r, hidden.marks, theirs=f"{hidden.word} seen at the shoot")
    found = _ok(env.client.get("/v1/query", params={"f": [f"query:{hidden.word}"]}, headers=hidden.key))
    assert [i["asset_id"] for i in found["items"]] == [hidden.a1], "their own library's clip, found by the word"
    assert found["items"][0]["search_context"]["snippet"] == f"{hidden.word} seen at the shoot", "by its own note"
    everyone = _ok(env.client.get("/v1/query", params={"f": [f"query:{hidden.word}"]}, headers=env.admin))
    assert hidden.c_clips[0] in {i["asset_id"] for i in everyone["items"]}, "an account admin's search finds it"


@pytest.mark.slow
def test_counts_are_counted_over_what_they_see(env, hidden):
    def clip_counts(headers, **params):
        rows = _ok(env.client.get("/v1/producers", params=params, headers=headers))["producers"]
        return next(p["counts"] for p in rows if p["artifact"] == "lumiverb.image.likeness")

    theirs = clip_counts(hidden.key)
    everyones = clip_counts(env.admin)
    in_c = clip_counts(env.admin, library_id=hidden.c)
    assert theirs["applicable"] == everyones["applicable"] - in_c["applicable"]
    assert theirs["failing"] == everyones["failing"] - in_c["failing"]
    assert in_c["failing"] == 1


@pytest.mark.slow
def test_what_acts_on_every_library_acts_on_the_ones_they_edit(env, hidden):
    _ok(env.client.post("/v1/producers/failures/retry", json={"all": True}, headers=hidden.key))
    with env.db.connect() as conn:
        retry_at = dict(conn.execute(text(
            "SELECT asset_id, attempts FROM artifact_lineage WHERE artifact = 'lumiverb.image.likeness' AND asset_id = ANY(:ids)"),
            {"ids": [hidden.a1, hidden.c_clips[0]]}).all())
    assert retry_at[hidden.a1] == 0 and retry_at[hidden.c_clips[0]] > 0, "C's failure is left as it was"
    library_c = _ok(env.client.get(f"/v1/libraries/{hidden.c}", headers=env.admin))
    report = _ok(env.client.post("/v1/changes", headers=hidden.key,
                                 json={"paths": [library_c["root_path"] + "/theirs.jpg"]}))
    assert report["accepted"] == 0 and report["unmatched"] == 1, "a change in a library they can't see isn't kept"


@pytest.mark.slow
def test_projects_show_only_the_clips_they_can_see(env, hidden):
    assert env.client.get(f"/v1/projects/{hidden.only_c}", headers=hidden.key).status_code == 404
    assert env.client.get(f"/v1/projects/{hidden.only_c}/assets", headers=hidden.key).status_code == 404
    mixed = _ok(env.client.get(f"/v1/projects/{hidden.mixed}", headers=hidden.key))
    assert mixed["asset_count"] == 1 and mixed["cover_asset_id"] == hidden.a1, mixed
    assert mixed["trashed_asset_count"] == 1, "theirs to restore: A's, not C's"
    clips = _ok(env.client.get(f"/v1/projects/{hidden.mixed}/assets", headers=hidden.key))
    assert [c["asset_id"] for c in clips["items"]] == [hidden.a1]
    for path, params in ((f"/v1/projects/{hidden.mixed}", {}), (f"/v1/projects/{hidden.mixed}/assets", {}),
                         (f"/v1/projects/{hidden.mixed}/export", {"format": "fcpxml"}),
                         (f"/v1/projects/{hidden.mixed}/export", {"format": "fcp7"}),
                         (f"/v1/projects/{hidden.theirs}/assets", {})):
        r = env.client.get(path, params=params, headers=hidden.key)
        assert r.status_code == 200, (path, r.text)
        _says_nothing_of(r, hidden.marks)
        if path.endswith("/export"):
            assert "a1.jpg" in r.text, "their own clip is exported"
    r = env.client.get("/v1/producers", params={"project_id": hidden.mixed}, headers=hidden.key)
    assert r.status_code == 200 and next(p for p in r.json()["producers"]
                                         if p["artifact"] == "lumiverb.image.likeness")["counts"]["applicable"] == 1
    everyone = _ok(env.client.get(f"/v1/projects/{hidden.mixed}", headers=env.admin))
    assert everyone["asset_count"] == 2 and everyone["cover_asset_id"] == hidden.c_clips[0]


@pytest.mark.slow
def test_their_reorder_leaves_what_they_cant_see_in_its_place(env, hidden):
    _ok(env.client.patch(f"/v1/projects/{hidden.theirs}/reorder", headers=hidden.key,
                         json={"asset_ids": [hidden.a2, hidden.a1]}))
    with env.db.connect() as conn:
        order = [r[0] for r in conn.execute(text(
            "SELECT asset_id FROM project_assets WHERE project_id = :p ORDER BY position"), {"p": hidden.theirs})]
    assert order == [hidden.a2, hidden.c_clips[0], hidden.a1]


@pytest.mark.slow
def test_a_public_project_shows_what_its_owner_can_still_see(env, hidden):
    page = _ok(env.client.get(f"/v1/public/projects/{hidden.theirs}"))
    assert page["asset_count"] == 2
    clips = _ok(env.client.get(f"/v1/public/projects/{hidden.theirs}/assets"))
    assert {c["asset_id"] for c in clips["items"]} == {hidden.a1, hidden.a2}
    shown = env.client.get(f"/v1/assets/{hidden.a1}", params={"public_project_id": hidden.theirs})
    assert shown.status_code == 200, shown.text
    hidden_clip = env.client.get(f"/v1/assets/{hidden.c_clips[0]}", params={"public_project_id": hidden.theirs})
    assert hidden_clip.status_code in (403, 404), hidden_clip.text


@pytest.mark.slow
def test_restoring_a_projects_clips_restores_the_ones_they_edit(env, hidden):
    _ok(env.client.post(f"/v1/projects/{hidden.mixed}/restore-clips", headers=hidden.key))
    with env.db.connect() as conn:
        gone = dict(conn.execute(text("SELECT asset_id, deleted_at IS NOT NULL FROM assets WHERE asset_id = ANY(:ids)"),
                                 {"ids": [hidden.a3, hidden.c_clips[1]]}).all())
    assert gone == {hidden.a3: False, hidden.c_clips[1]: True}


@pytest.mark.slow
def test_people_show_only_the_faces_they_can_see(env, hidden):
    for method, path, body in (("GET", f"/v1/people/{hidden.only_c_person}", None),
                               ("GET", f"/v1/people/{hidden.only_c_person}/faces", None),
                               ("PATCH", f"/v1/people/{hidden.only_c_person}", {"display_name": "Renamed"}),
                               ("DELETE", f"/v1/people/{hidden.only_c_person}", None),
                               ("POST", f"/v1/people/{hidden.both}/merge", {"source_person_id": hidden.only_c_person}),
                               ("POST", f"/v1/people/{hidden.dismissed}/undismiss", {"display_name": "Them"}),
                               ("POST", f"/v1/faces/{hidden.face['a2']}/assign", {"person_id": hidden.only_c_person})):
        r = env.client.request(method, path, json=body, headers=hidden.key)
        assert r.status_code == 404, (method, path, r.text)
    shown = _ok(env.client.get(f"/v1/people/{hidden.both}", headers=hidden.key))
    assert shown["face_count"] == 1 and shown["representative_face_id"] == hidden.face["a"], shown
    assert shown["representative_asset_id"] == hidden.a1
    faces = _ok(env.client.get(f"/v1/people/{hidden.both}/faces", headers=hidden.key))
    assert [f["face_id"] for f in faces["items"]] == [hidden.face["a"]]
    listed = {p["person_id"]: p for p in _ok(env.client.get("/v1/people", headers=hidden.key))["items"]}
    assert hidden.both in listed and hidden.only_c_person not in listed
    assert listed[hidden.both]["face_count"] == 1
    for path in (f"/v1/people/{hidden.both}/nearest", f"/v1/faces/{hidden.face['a2']}/nearest-people"):
        r = env.client.get(path, headers=hidden.key)
        assert r.status_code == 200, (path, r.text)
        _says_nothing_of(r, hidden.marks)
    everyone = _ok(env.client.get(f"/v1/people/{hidden.both}", headers=env.admin))
    assert everyone["face_count"] == 2 and everyone["representative_face_id"] == hidden.face["c5"]


@pytest.mark.slow
def test_groups_of_faces_show_only_the_faces_they_can_see(env, hidden):
    groups = _ok(env.client.get("/v1/faces/clusters", params={"min_cluster_size": 1}, headers=hidden.key))
    assert [(g["cluster_id"], g["size"], [f["face_id"] for f in g["faces"]]) for g in groups["clusters"]] == [
        (hidden.both_group, 1, [hidden.face["a2"]])]
    faces = _ok(env.client.get(f"/v1/faces/clusters/{hidden.both_group}/faces", headers=hidden.key))
    assert [f["face_id"] for f in faces["items"]] == [hidden.face["a2"]] and faces["total"] == 1
    for path in (f"/v1/faces/clusters/{hidden.c_group}/faces", f"/v1/faces/clusters/{hidden.c_group}/nearest-people"):
        r = env.client.get(path, headers=hidden.key)
        assert r.status_code == 409 and r.json()["error"]["code"] == "cluster_changed", (path, r.text)
    named = _ok(env.client.post(f"/v1/faces/clusters/{hidden.both_group}/name", headers=hidden.key,
                                json={"display_name": "Named by them"}), 201)
    with env.db.connect() as conn:
        theirs = {r[0] for r in conn.execute(text("SELECT face_id FROM face_person_matches WHERE person_id = :p"),
                                             {"p": named["person_id"]})}
    assert theirs == {hidden.face["a2"]}, "C's face in the group is left as it was"
    everyone = _ok(env.client.get("/v1/faces/clusters", params={"min_cluster_size": 1}, headers=env.admin))
    assert hidden.c_group in {g["cluster_id"] for g in everyone["clusters"]}
