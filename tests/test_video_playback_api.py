"""Video playback: full length by default, or capped by an account setting.

The player streams a video's analysis proxy (full length, every audio
track) through a short-lived signed link, since a <video> element can't send
an Authorization header. Before the proxy exists, the link plays the
10-second preview scan makes. An admin can cap playback at N seconds; the
server serves only the first N seconds, so the cap holds for public pages.

Uses testcontainers Postgres (control + tenant), a temp LocalStorage and
real MP4s made with ffmpeg.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from src.server.storage.local import LocalStorage
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations

_STORAGE_USERS = ("artifacts", "assets", "trash", "ingest", "playback")


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, bytes]:
    d = tmp_path_factory.mktemp("media")

    def make(name: str, seconds: float, tracks: int) -> bytes:
        out = d / name
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={seconds}"]
        for i in range(tracks):
            cmd += ["-f", "lavfi", "-i", f"sine=frequency={440 + 220 * i}:duration={seconds}"]
        cmd += ["-map", "0:v", *[a for i in range(tracks) for a in ("-map", f"{i + 1}:a")],
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-movflags", "+faststart", str(out)]
        subprocess.run(cmd, check=True)
        return out.read_bytes()

    return {"proxy": make("proxy.mp4", 6.0, 2), "preview": make("preview.mp4", 3.0, 1)}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Yield (client, headers, library_id, storage, keys) where keys maps role -> headers."""
    storage = LocalStorage(str(tmp_path_factory.mktemp("playback_storage")))
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer(PG_IMAGE) as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)

        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(make_url(control_url).set(database="{tenant_id}"))
        os.environ["ADMIN_KEY"] = "test-admin-playback"
        os.environ.setdefault("JWT_SECRET", "x")
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"), TestClient(app) as client:
            r = client.post("/v1/admin/tenants", json={"name": "PlaybackTenant", "plan": "free"},
                            headers={"Authorization": "Bearer test-admin-playback"})
            assert r.status_code == 200, r.text
            tenant_id, api_key = r.json()["tenant_id"], r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with ExitStack() as stack:
                for mod in _STORAGE_USERS:
                    stack.enter_context(patch(f"src.server.api.routers.{mod}.get_storage", return_value=storage))
                with TestClient(app) as client:
                    admin = {"Authorization": f"Bearer {api_key}"}
                    keys = {"admin": admin}
                    for role in ("editor", "viewer"):
                        r = client.post("/v1/keys", json={"label": role, "role": role}, headers=admin)
                        assert r.status_code == 200, r.text
                        keys[role] = {"Authorization": f"Bearer {r.json()['plaintext']}"}
                    r = client.post("/v1/libraries", json={"name": "Footage", "root_path": "/Volumes/media-01/Footage"},
                                    headers=admin)
                    assert r.status_code == 200, r.text
                    yield client, admin, r.json()["library_id"], storage, keys
        _engines.clear()


@pytest.fixture(autouse=True)
def _no_cap(env):
    """Each test starts with full-length playback."""
    client, admin, *_ = env
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": None}, headers=admin)
    yield


def _ingest(env, rel_path: str, media_type: str = "video") -> str:
    client, headers, library_id, *_ = env
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color=(10, 20, 30)).save(buf, format="JPEG")
    buf.seek(0)
    data = {"library_id": library_id, "rel_path": rel_path, "file_size": "1000", "media_type": media_type,
            "width": "64", "height": "36", "exif": json.dumps({"sha256": os.urandom(32).hex()})}
    r = client.post("/v1/ingest", data=data, files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _upload(env, asset_id: str, artifact_type: str, body: bytes) -> None:
    client, headers, *_ = env
    r = client.post(f"/v1/assets/{asset_id}/artifacts/{artifact_type}",
                    files={"file": ("a.mp4", io.BytesIO(body), "video/mp4")}, headers=headers)
    assert r.status_code == 200, r.text


def _video(env, media, name: str, *, proxy: bool = True, preview: bool = True) -> str:
    asset_id = _ingest(env, f"{name}.mov")
    if preview:
        _upload(env, asset_id, "video_preview", media["preview"])
    if proxy:
        _upload(env, asset_id, "analysis_proxy", media["proxy"])
    return asset_id


def _playback(env, asset_id: str, headers=None, params=None):
    client, admin, *_ = env
    return client.get(f"/v1/assets/{asset_id}/playback", headers=admin if headers is None else headers,
                      params=params or {})


def _path(url: str) -> str:
    u = urlparse(url)
    return u.path + (f"?{u.query}" if u.query else "")


def _duration(body: bytes, tmp_path: Path) -> float:
    f = tmp_path / "got.mp4"
    f.write_bytes(body)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(f)],
                         check=True, capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


def _audio_tracks(body: bytes, tmp_path: Path) -> int:
    f = tmp_path / "tracks.mp4"
    f.write_bytes(body)
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "json", str(f)], check=True, capture_output=True, text=True).stdout
    return len(json.loads(out)["streams"])


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_full_length_is_the_default(env):
    client, admin, *_ = env
    r = client.get("/v1/tenant/settings", headers=admin)
    assert r.status_code == 200
    assert r.json() == {"video_preview_max_seconds": None}


@pytest.mark.slow
def test_an_admin_sets_and_clears_the_cap(env):
    client, admin, *_ = env
    r = client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 30}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["video_preview_max_seconds"] == 30
    assert client.get("/v1/tenant/settings", headers=admin).json()["video_preview_max_seconds"] == 30
    r = client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": None}, headers=admin)
    assert r.json()["video_preview_max_seconds"] is None


@pytest.mark.slow
@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_only_admins_change_it_but_everyone_reads_it(env, role):
    client, _admin, _lib, _storage, keys = env
    r = client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 5}, headers=keys[role])
    assert r.status_code == 403
    assert client.get("/v1/tenant/settings", headers=keys[role]).status_code == 200


@pytest.mark.slow
@pytest.mark.parametrize("bad", [0, -1, 86_401, "ten", 2.5])
def test_nonsense_caps_are_refused(env, bad):
    client, admin, *_ = env
    r = client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": bad}, headers=admin)
    assert r.status_code == 422


@pytest.mark.slow
def test_leaving_the_field_out_changes_nothing(env):
    client, admin, *_ = env
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 12}, headers=admin)
    r = client.patch("/v1/tenant/settings", json={}, headers=admin)
    assert r.json()["video_preview_max_seconds"] == 12


# ---------------------------------------------------------------------------
# Playback links
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_full_length_proxy_streams_with_every_audio_track(env, media, tmp_path):
    client, *_ = env
    asset_id = _video(env, media, "full")
    r = _playback(env, asset_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "analysis_proxy"
    assert body["max_seconds"] is None
    assert body["expires_at"]
    # No Authorization header: the signature is the credential.
    got = client.get(_path(body["url"]))
    assert got.status_code == 200
    assert got.headers["content-type"] == "video/mp4"
    assert got.content == media["proxy"]
    assert _audio_tracks(got.content, tmp_path) == 2


@pytest.mark.slow
def test_the_link_seeks(env, media):
    client, *_ = env
    url = _playback(env, _video(env, media, "seek")).json()["url"]
    got = client.get(_path(url), headers={"Range": "bytes=100-199"})
    assert got.status_code == 206
    assert got.content == media["proxy"][100:200]
    assert got.headers["accept-ranges"] == "bytes"


@pytest.mark.slow
def test_before_the_proxy_exists_the_preview_plays(env, media):
    client, *_ = env
    asset_id = _video(env, media, "early", proxy=False)
    body = _playback(env, asset_id).json()
    assert body["source"] == "preview"
    assert client.get(_path(body["url"])).content == media["preview"]


@pytest.mark.slow
def test_nothing_to_play(env, media):
    asset_id = _video(env, media, "nothing", proxy=False, preview=False)
    assert _playback(env, asset_id).status_code == 404


@pytest.mark.slow
def test_photos_have_no_playback(env):
    assert _playback(env, _ingest(env, "still.jpg", media_type="image")).status_code == 422


@pytest.mark.slow
def test_a_trashed_video_stops_streaming(env, media):
    client, admin, *_ = env
    asset_id = _video(env, media, "trashed")
    url = _playback(env, asset_id).json()["url"]
    r = client.delete(f"/v1/assets/{asset_id}", headers=admin)
    assert r.status_code == 204, r.text
    assert client.get(_path(url)).status_code == 404
    assert _playback(env, asset_id).status_code == 404


@pytest.mark.slow
def test_a_tampered_link_is_refused(env, media):
    client, *_ = env
    a = _video(env, media, "mine")
    b = _video(env, media, "other")
    url_a = _path(_playback(env, a).json()["url"])
    token = url_a.rsplit("/", 1)[1]
    payload, sig = token.split(".")
    assert client.get(url_a.replace(sig, sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB"))).status_code == 403
    # Someone else's asset id with this signature
    import base64

    forged = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    forged["a"] = b
    fp = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
    assert client.get(url_a.replace(payload, fp)).status_code == 403
    assert client.get("/v1/stream/garbage").status_code == 403


@pytest.mark.slow
def test_an_expired_link_is_refused(env, media):
    client, *_ = env
    from src.server.api.routers.playback import mint_stream_token

    asset_id = _video(env, media, "expired")
    tenant_id = client.get("/v1/tenant/context", headers=env[1]).json()["tenant_id"]
    token = mint_stream_token(tenant_id, asset_id, ttl_seconds=-1)
    assert client.get(f"/v1/stream/{token}").status_code == 403
    fresh = mint_stream_token(tenant_id, asset_id, ttl_seconds=60)
    assert client.get(f"/v1/stream/{fresh}").status_code == 200


@pytest.mark.slow
def test_viewers_can_play(env, media):
    client, _admin, _lib, _storage, keys = env
    body = _playback(env, _video(env, media, "viewer"), headers=keys["viewer"]).json()
    assert client.get(_path(body["url"])).status_code == 200


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_cap_serves_only_the_first_seconds(env, media, tmp_path):
    client, admin, *_ = env
    asset_id = _video(env, media, "capped")
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 2}, headers=admin)
    body = _playback(env, asset_id).json()
    assert body["max_seconds"] == 2
    got = client.get(_path(body["url"]))
    assert got.status_code == 200
    assert _duration(got.content, tmp_path) == pytest.approx(2.0, abs=0.6)
    assert _audio_tracks(got.content, tmp_path) == 2
    ranged = client.get(_path(body["url"]), headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and ranged.content == got.content[:100]


@pytest.mark.slow
def test_a_cap_longer_than_the_video_serves_it_whole(env, media):
    client, admin, *_ = env
    asset_id = _video(env, media, "short")
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 600}, headers=admin)
    got = client.get(_path(_playback(env, asset_id).json()["url"]))
    assert got.content == media["proxy"]


@pytest.mark.slow
def test_a_new_cap_applies_to_links_already_handed_out(env, media, tmp_path):
    client, admin, *_ = env
    url = _path(_playback(env, _video(env, media, "change")).json()["url"])
    assert client.get(url).content == media["proxy"]
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 1}, headers=admin)
    assert _duration(client.get(url).content, tmp_path) == pytest.approx(1.0, abs=0.6)


@pytest.mark.slow
def test_the_cap_applies_to_the_preview_too(env, media, tmp_path):
    client, admin, *_ = env
    asset_id = _video(env, media, "preview_capped", proxy=False)
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 1}, headers=admin)
    got = client.get(_path(_playback(env, asset_id).json()["url"]))
    assert _duration(got.content, tmp_path) == pytest.approx(1.0, abs=0.6)
    hover = client.get(f"/v1/assets/{asset_id}/preview", headers=admin)
    assert _duration(hover.content, tmp_path) == pytest.approx(1.0, abs=0.6)


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_public_library_playback_and_its_end(env, media):
    client, admin, library_id, *_ = env
    asset_id = _video(env, media, "public")
    assert client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=admin).status_code == 200
    try:
        r = client.get(f"/v1/assets/{asset_id}/playback", params={"public_library_id": library_id})
        assert r.status_code == 200, r.text
        url = _path(r.json()["url"])
        assert client.get(url).status_code == 200
        # The full analysis proxy isn't downloadable from a public page.
        r = client.get(f"/v1/assets/{asset_id}/artifacts/analysis_proxy", params={"public_library_id": library_id})
        assert r.status_code == 403
    finally:
        client.patch(f"/v1/libraries/{library_id}", json={"is_public": False}, headers=admin)
    # Made private again: the link handed to the public stops working.
    assert client.get(url).status_code == 404


@pytest.mark.slow
def test_signed_in_users_still_download_the_proxy(env, media):
    client, admin, *_ = env
    asset_id = _video(env, media, "download")
    r = client.get(f"/v1/assets/{asset_id}/artifacts/analysis_proxy", headers=admin)
    assert r.status_code == 200 and r.content == media["proxy"]


@pytest.mark.slow
def test_links_expire_in_hours_not_days(env, media):
    body = _playback(env, _video(env, media, "ttl")).json()
    from datetime import datetime

    expires = datetime.fromisoformat(body["expires_at"]).timestamp()
    assert 3600 <= expires - time.time() <= 24 * 3600
