"""A public visitor never sees an email address, anywhere (Robert's rule).

Seeds a public library and a public project whose data carries emails
wherever one can live: a note (note_author), a person's location, a
correction, a transcript, the project's owner, the file's EXIF (Artist,
Copyright) and the users who did it. Then calls every GET route a visitor
can reach, found from the app's routes and the middleware's public
allow-list (so a new public route is covered by itself), and asserts no
response body or header holds an email address.
"""

from __future__ import annotations

import io
import json
import os
import re

import pytest
from fastapi.routing import APIRoute
from PIL import Image

from src.server.api.main import app
from src.server.api.middleware import _PUBLIC_ELIGIBLE_PATH
from tests.machine_lineage import ingest_made
from tests.test_public_library_middleware import public_lib_client  # noqa: F401 — the shared server fixture

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
EDITOR = "seed.editor@example.com"
ARTIST = "photographer@example.org"


def public_get_routes() -> list[str]:
    """Every GET route an unauthenticated visitor's request can reach."""
    out = []
    for r in app.routes:
        if not isinstance(r, APIRoute) or "GET" not in r.methods:
            continue
        sample = re.sub(r"\{[^}]+\}", "x", r.path)
        if _PUBLIC_ELIGIBLE_PATH.match(sample) or sample.startswith(("/v1/public/projects/", "/v1/stream/")):
            out.append(r.path)
    return out


def _ingest(client, auth, library_id: str, rel_path: str, media_type: str) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (32, 18), color=(40, 50, 60)).save(buf, format="JPEG")
    buf.seek(0)
    exif = {"sha256": os.urandom(32).hex(), "gps_lat": 40.7, "gps_lon": -74.0, "camera_make": "Acme",
            "exif": {"Artist": ARTIST, "Copyright": f"(c) {ARTIST}"}}
    r = client.post("/v1/ingest", data={"library_id": library_id, "rel_path": rel_path, "file_size": "10",
                                        "media_type": media_type, "width": "32", "height": "18",
                                        "exif": json.dumps(exif), "lineage": ingest_made()},
                    files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


@pytest.fixture(scope="module")
def seeded(public_lib_client):
    client, api_key, library_id, private_library_id = public_lib_client
    admin = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/v1/users", json={"email": EDITOR, "password": "correct-horse-battery-staple", "role": "editor"},
                    headers=admin)
    assert r.status_code == 201, r.text
    r = client.post("/v1/auth/login", json={"email": EDITOR, "password": "correct-horse-battery-staple"})
    assert r.status_code == 200, r.text
    editor = {"Authorization": f"Bearer {r.json()['access_token']}"}

    photo = _ingest(client, admin, library_id, "emails/photo.jpg", "image")
    clip = _ingest(client, admin, library_id, "emails/clip.mov", "video")
    shared = _ingest(client, admin, private_library_id, "emails/shared.jpg", "image")
    for asset_id in (photo, clip, shared):
        assert client.put(f"/v1/assets/{asset_id}/note", json={"text": f"ask {EDITOR}"},
                          headers=editor).status_code == 200
        assert client.patch(f"/v1/assets/{asset_id}/corrections", json={"description": "A street."},
                            headers=editor).status_code == 200
    r = client.put("/v1/assets/locations", json={"asset_ids": [photo, clip, shared], "lat": 1.5, "lon": 2.5,
                                                 "replace": "all"}, headers=editor)
    assert r.status_code == 200, r.text
    srt = "1\n00:00:01,000 --> 00:00:02,000\nhello\n"
    assert client.post(f"/v1/assets/{clip}/transcript", json={"source": "manual", "srt": srt, "language": "en"},
                       headers=editor).status_code == 200
    r = client.post("/v1/projects", json={"name": "Reel", "asset_ids": [shared, photo], "visibility": "public"},
                    headers=editor)
    assert r.status_code == 201, r.text
    # Signed in, the email is there to find (the test would prove nothing otherwise).
    detail = client.get(f"/v1/assets/{photo}", headers=editor).json()
    assert detail["note_author"] == EDITOR and detail["location"]["set_by"] == EDITOR
    return {"library_id": library_id, "project_id": r.json()["project_id"], "assets": (photo, clip, shared),
            "client": client}


def _urls(path: str, s: dict, asset_id: str) -> list[str]:
    url = (path.replace("{library_id}", s["library_id"]).replace("{asset_id}", asset_id)
           .replace("{project_id}", s["project_id"]).replace("{artifact_type}", "proxy"))
    return [re.sub(r"\{[^}]+\}", "x", url)]


def _param_sets(s: dict, asset_id: str) -> list[list[tuple[str, str]]]:
    lib, proj = s["library_id"], s["project_id"]
    return [
        [("public_library_id", lib), ("library_id", lib), ("f", f"library:{lib}"), ("asset_id", asset_id),
         ("rel_path", "emails/photo.jpg")],
        [("public_library_id", lib), ("library_id", lib), ("f", f"library:{lib}"), ("f", "query:street")],
        [("public_project_id", proj), ("asset_id", asset_id)],
    ]


@pytest.mark.slow
def test_no_public_response_holds_an_email(seeded):
    client = seeded["client"]
    routes = public_get_routes()
    # The allow-list as it stands: if these go, the enumeration broke.
    for must in ("/v1/query", "/v1/assets/page", "/v1/assets/{asset_id}", "/v1/assets/by-path", "/v1/similar",
                 "/v1/libraries/{library_id}/directories", "/v1/public/projects/{project_id}",
                 "/v1/public/projects/{project_id}/assets", "/v1/assets/{asset_id}/playback",
                 "/v1/stream/{token}", "/v1/assets/facets"):
        assert must in routes, must

    answered: set[str] = set()
    leaks = []
    for path in routes:
        for asset_id in seeded["assets"]:
            for url in _urls(path, seeded, asset_id):
                for params in _param_sets(seeded, asset_id):
                    r = client.get(url, params=params)
                    if r.status_code == 200:
                        answered.add(path)
                    seen = [f"{k}: {v}" for k, v in r.headers.items()]
                    ctype = r.headers.get("content-type", "")
                    if "json" in ctype or ctype.startswith("text/"):
                        seen.append(r.text)
                    if r.status_code == 200 and path == "/v1/assets/{asset_id}/playback":
                        link = r.json().get("url") or ""
                        if link:
                            s = client.get(link)
                            seen += [f"{k}: {v}" for k, v in s.headers.items()]
                    for text in seen:
                        if EMAIL.search(text) or EDITOR in text or ARTIST in text:
                            leaks.append(f"{url} {params} -> {r.status_code}: {EMAIL.search(text)}")
    assert not leaks, "Emails on public responses:\n" + "\n".join(leaks)
    # The routes that matter answered the visitor (a test of 401s would prove nothing).
    for must in ("/v1/query", "/v1/assets/page", "/v1/assets/{asset_id}",  # by-path refuses visitors (403)
                 "/v1/public/projects/{project_id}", "/v1/public/projects/{project_id}/assets", "/v1/assets/facets",
                 "/v1/libraries/{library_id}/directories", "/v1/libraries/{library_id}"):
        assert must in answered, (must, sorted(answered))
