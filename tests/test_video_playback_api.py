"""Video playback: full length by default, or capped by an account setting.

The player streams a video's analysis proxy (full length, every audio
track) through a short-lived signed link, since a <video> element can't send
an Authorization header. Before the proxy exists, the link plays the
10-second preview scan makes. An admin can cap playback at N seconds; the
server serves only the first N seconds. Public pages have their own cap,
10 seconds unless an admin raises it, and never more than the account's.

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
from tests.machine_lineage import ingest_made, made_json

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

    return {"proxy": make("proxy.mp4", 6.0, 2), "preview": make("preview.mp4", 3.0, 1),
            "long": make("long.mp4", 14.0, 2)}


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
def _defaults(env):
    """Each test starts with the defaults: whole videos signed in, 10 s on public pages, library private."""
    client, admin, library_id, *_ = env
    r = client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": None,
                                                   "public_video_preview_max_seconds": 10}, headers=admin)
    assert r.status_code == 200, r.text
    client.patch(f"/v1/libraries/{library_id}", json={"is_public": False}, headers=admin)
    yield


def _ingest(env, rel_path: str, media_type: str = "video") -> str:
    client, headers, library_id, *_ = env
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color=(10, 20, 30)).save(buf, format="JPEG")
    buf.seek(0)
    data = {"library_id": library_id, "rel_path": rel_path, "file_size": "1000", "media_type": media_type,
            "width": "64", "height": "36", "exif": json.dumps({"sha256": os.urandom(32).hex()}),
            "lineage": ingest_made()}
    r = client.post("/v1/ingest", data=data, files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _upload(env, asset_id: str, artifact_type: str, body: bytes) -> None:
    client, headers, *_ = env
    r = client.post(f"/v1/assets/{asset_id}/artifacts/{artifact_type}",
                    files={"file": ("a.mp4", io.BytesIO(body), "video/mp4")},
                    data={"lineage": made_json(artifact_type)}, headers=headers)
    assert r.status_code == 200, r.text


def _video(env, media, name: str, *, proxy: bool = True, preview: bool = True, long: bool = False) -> str:
    asset_id = _ingest(env, f"{name}.mov")
    if preview:
        _upload(env, asset_id, "video_preview", media["preview"])
    if proxy:
        _upload(env, asset_id, "analysis_proxy", media["long" if long else "proxy"])
    return asset_id


def _cuts(env) -> list[str]:
    client, admin, _lib, storage, _keys = env
    tenant_id = client.get("/v1/tenant/context", headers=admin).json()["tenant_id"]
    d = storage.abs_path(f"{tenant_id}/playback")
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def _public(env):
    client, admin, library_id, *_ = env
    assert client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=admin).status_code == 200
    return {"public_library_id": library_id}


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
def test_full_length_is_the_default_and_public_pages_get_10_seconds(env):
    client, admin, *_ = env
    r = client.get("/v1/tenant/settings", headers=admin)
    assert r.status_code == 200
    assert r.json() == {"video_preview_max_seconds": None, "public_video_preview_max_seconds": 10, "follow_moves": True,
                        "trash_days": 30}


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
@pytest.mark.parametrize("field", ["video_preview_max_seconds", "public_video_preview_max_seconds"])
@pytest.mark.parametrize("bad", [0, -1, 86_401, "ten", 2.5])
def test_nonsense_caps_are_refused(env, field, bad):
    client, admin, *_ = env
    r = client.patch("/v1/tenant/settings", json={field: bad}, headers=admin)
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
def test_links_last_about_an_hour(env, media):
    # Short-lived: a copied link stops working soon; the player renews on error.
    body = _playback(env, _video(env, media, "ttl")).json()
    from datetime import datetime

    expires = datetime.fromisoformat(body["expires_at"]).timestamp()
    assert 1800 <= expires - time.time() <= 2 * 3600


# ---------------------------------------------------------------------------
# Public pages: their own cap
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_public_pages_play_10_seconds_by_default(env, media, tmp_path):
    client, admin, *_ = env
    asset_id = _video(env, media, "pub_default", long=True)
    params = _public(env)
    body = client.get(f"/v1/assets/{asset_id}/playback", params=params).json()
    assert body["max_seconds"] == 10
    assert _duration(client.get(_path(body["url"])).content, tmp_path) == pytest.approx(10, abs=0.6)
    # Signed in, the same video plays whole.
    signed_in = _playback(env, asset_id).json()
    assert signed_in["max_seconds"] is None
    assert _duration(client.get(_path(signed_in["url"])).content, tmp_path) == pytest.approx(14, abs=0.6)


@pytest.mark.slow
def test_an_admin_raises_or_lifts_the_public_cap(env, media, tmp_path):
    client, admin, *_ = env
    asset_id = _video(env, media, "pub_raise", long=True)
    params = _public(env)
    client.patch("/v1/tenant/settings", json={"public_video_preview_max_seconds": 12}, headers=admin)
    url = _path(client.get(f"/v1/assets/{asset_id}/playback", params=params).json()["url"])
    assert _duration(client.get(url).content, tmp_path) == pytest.approx(12, abs=0.6)
    client.patch("/v1/tenant/settings", json={"public_video_preview_max_seconds": None}, headers=admin)
    assert client.get(url).content == media["long"]


@pytest.mark.slow
def test_the_account_cap_limits_public_pages_too(env, media, tmp_path):
    client, admin, *_ = env
    asset_id = _video(env, media, "pub_account", long=True)
    params = _public(env)
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 3}, headers=admin)
    body = client.get(f"/v1/assets/{asset_id}/playback", params=params).json()
    assert body["max_seconds"] == 3
    assert _duration(client.get(_path(body["url"])).content, tmp_path) == pytest.approx(3, abs=0.6)


@pytest.mark.slow
def test_public_hover_and_preview_artifact_follow_the_public_cap(env, media, tmp_path):
    client, admin, *_ = env
    asset_id = _video(env, media, "pub_hover", proxy=False)
    params = _public(env)
    client.patch("/v1/tenant/settings", json={"public_video_preview_max_seconds": 1}, headers=admin)
    hover = client.get(f"/v1/assets/{asset_id}/preview", params=params)
    assert _duration(hover.content, tmp_path) == pytest.approx(1, abs=0.6)
    art = client.get(f"/v1/assets/{asset_id}/artifacts/video_preview", params=params)
    assert _duration(art.content, tmp_path) == pytest.approx(1, abs=0.6)
    # Signed in, the preview is whole.
    assert client.get(f"/v1/assets/{asset_id}/artifacts/video_preview", headers=admin).content == media["preview"]


@pytest.mark.slow
def test_public_transcripts_stop_at_the_public_cap(env, media):
    client, admin, *_ = env
    asset_id = _video(env, media, "pub_transcript", long=True)
    srt = ("1\n00:00:02,000 --> 00:00:04,000\nearly words\n\n"
           "2\n00:00:09,500 --> 00:00:12,000\nstraddling\n\n"
           "3\n00:00:20,000 --> 00:00:22,000\nlate secret\n")
    r = client.post(f"/v1/assets/{asset_id}/transcript", json={"source": "manual", "srt": srt, "language": "en"}, headers=admin)
    assert r.status_code == 200, r.text
    params = _public(env)
    public = client.get(f"/v1/assets/{asset_id}", params=params).json()["transcript_srt"]
    assert "early words" in public and "straddling" in public and "late secret" not in public
    assert "late secret" in client.get(f"/v1/assets/{asset_id}", headers=admin).json()["transcript_srt"]


@pytest.mark.slow
def test_both_public_params_at_once_are_refused(env, media):
    client, *_ = env
    asset_id = _video(env, media, "pub_both")
    params = {**_public(env), "public_project_id": "prj_nope"}
    assert client.get(f"/v1/assets/{asset_id}/playback", params=params).status_code == 400


# ---------------------------------------------------------------------------
# Cuts don't outlive what they were cut from
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_changing_a_cap_clears_the_old_cuts(env, media):
    client, admin, *_ = env
    asset_id = _video(env, media, "cut_clear")
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 2}, headers=admin)
    client.get(_path(_playback(env, asset_id).json()["url"]))
    assert any(n.startswith(asset_id) for n in _cuts(env))
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": None}, headers=admin)
    assert not any(n.startswith(asset_id) for n in _cuts(env))


@pytest.mark.slow
def test_deleting_for_good_removes_its_cuts(env, media):
    client, admin, *_ = env
    asset_id = _video(env, media, "cut_purge")
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 2}, headers=admin)
    client.get(_path(_playback(env, asset_id).json()["url"]))
    assert any(n.startswith(asset_id) for n in _cuts(env))
    assert client.delete(f"/v1/assets/{asset_id}", headers=admin).status_code == 204
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [asset_id]}, headers=admin)
    assert r.status_code == 200, r.text
    assert not any(n.startswith(asset_id) for n in _cuts(env))


@pytest.mark.slow
def test_a_cut_that_cant_be_made_is_a_503(env, media, monkeypatch):
    client, admin, *_ = env
    asset_id = _video(env, media, "cut_fail")
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 2}, headers=admin)
    url = _path(_playback(env, asset_id).json()["url"])
    from src.server.api.routers import playback

    real = playback.subprocess.run
    monkeypatch.setattr(playback.subprocess, "run",
                        lambda cmd, *a, **k: real(["false"], *a, **k) if cmd[0] == "ffmpeg" else real(cmd, *a, **k))
    r = client.get(url)
    assert r.status_code == 503
    assert not any(n.startswith(asset_id) or n.startswith(f".{asset_id}") for n in _cuts(env))


# ---------------------------------------------------------------------------
# No way around the cap
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_viewers_cant_download_the_whole_proxy_while_capped(env, media):
    client, _admin, _lib, _storage, keys = env
    asset_id = _video(env, media, "bypass")
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 2}, headers=keys["admin"])
    url = f"/v1/assets/{asset_id}/artifacts/analysis_proxy"
    assert client.get(url, headers=keys["viewer"]).status_code == 403
    assert client.get(url, headers=keys["editor"]).status_code == 200  # the brain's tools need it
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": None}, headers=keys["admin"])
    assert client.get(url, headers=keys["viewer"]).status_code == 200


@pytest.mark.slow
def test_a_signed_in_preview_artifact_follows_the_account_cap(env, media, tmp_path):
    client, admin, *_ = env
    asset_id = _video(env, media, "art_cap", proxy=False)
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 1}, headers=admin)
    art = client.get(f"/v1/assets/{asset_id}/artifacts/video_preview", headers=admin)
    assert _duration(art.content, tmp_path) == pytest.approx(1, abs=0.6)


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_ranges_from_the_end_and_past_the_end(env, media):
    client, *_ = env
    url = _path(_playback(env, _video(env, media, "ranges")).json()["url"])
    size = len(media["proxy"])
    tail = client.get(url, headers={"Range": "bytes=-100"})
    assert tail.status_code == 206 and tail.content == media["proxy"][-100:]
    assert tail.headers["content-range"] == f"bytes {size - 100}-{size - 1}/{size}"
    past = client.get(url, headers={"Range": f"bytes={size + 10}-"})
    assert past.status_code == 416
    assert past.headers["content-range"] == f"bytes */{size}"


@pytest.mark.slow
def test_the_stream_names_its_version(env, media):
    # A cap change or a new proxy mid-play mustn't splice two files together.
    client, admin, *_ = env
    url = _path(_playback(env, _video(env, media, "etag")).json()["url"])
    whole = client.get(url).headers["etag"]
    client.patch("/v1/tenant/settings", json={"video_preview_max_seconds": 2}, headers=admin)
    assert client.get(url).headers["etag"] != whole


@pytest.mark.fast
@pytest.mark.parametrize("token", ["", ".", "abc", "abc.", ".abc", "a.b.c", "é.é", "W10.x", "bnVsbA.x"])
def test_malformed_tokens_read_as_nothing(token, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x")
    get_settings.cache_clear()
    from src.server.api.routers.playback import read_stream_token

    assert read_stream_token(token) is None


@pytest.mark.fast
def test_the_daily_sweep_drops_cuts_of_gone_assets_and_stale_temp_files(tmp_path):
    from src.server.api.routers.playback import sweep_cuts

    kept = "ast_01M4CWVZ2MPTAV6FSGG6B6VXVP"
    gone = "ast_01M4CWVZYN681BTVTSA2CYJQNZ"
    names = [f"{kept}_analysis_proxy_abc_10s.mp4", f"{gone}_preview_1-2_5s.mp4",
             f".{kept}_fresh.mp4", f".{gone}_old.mp4", "stray.txt"]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    old = time.time() - 3600
    os.utime(tmp_path / f".{gone}_old.mp4", (old, old))

    assert sweep_cuts(tmp_path, {kept}, dry_run=True) == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names)
    assert sweep_cuts(tmp_path, {kept}, dry_run=False) == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([f"{kept}_analysis_proxy_abc_10s.mp4",
                                                                  f".{kept}_fresh.mp4"])
    assert sweep_cuts(tmp_path / "missing", {kept}, dry_run=False) == 0


@pytest.mark.fast
@pytest.mark.parametrize("dry_run", [True, False])
def test_the_cleanup_job_sweeps_playback_cuts(tmp_path, dry_run):
    from unittest.mock import MagicMock

    from src.server.search.cleanup import run_cleanup_for_tenant

    kept, gone = "ast_01M4CWVZ2MPTAV6FSGG6B6VXVP", "ast_01M4CWVZYN681BTVTSA2CYJQNZ"
    folder = tmp_path / "ten_1" / "playback"
    folder.mkdir(parents=True)
    (folder / f"{kept}_preview_1-2_5s.mp4").write_bytes(b"x")
    (folder / f"{gone}_preview_1-2_5s.mp4").write_bytes(b"x")

    def execute(sql, *a, **k):
        rows = [(kept,)] if "FROM assets" in str(sql) else []
        return MagicMock(fetchall=lambda: rows)

    session = MagicMock(execute=execute)
    result = run_cleanup_for_tenant(tmp_path, "ten_1", session, dry_run=dry_run)
    assert result.orphan_files == 1
    left = sorted(p.name for p in folder.iterdir())
    assert left == sorted([f"{kept}_preview_1-2_5s.mp4"] + ([f"{gone}_preview_1-2_5s.mp4"] if dry_run else []))


@pytest.mark.slow
def test_public_project_pages_play_hover_and_show_details(env, media, tmp_path):
    client, admin, *_ = env
    asset_id = _video(env, media, "prj_clip", long=True)
    outsider = _video(env, media, "prj_outsider")
    r = client.post("/v1/projects", json={"name": "Public reel", "asset_ids": [asset_id], "visibility": "public"},
                    headers=admin)
    assert r.status_code == 201, r.text
    params = {"public_project_id": r.json()["project_id"]}

    body = client.get(f"/v1/assets/{asset_id}/playback", params=params)
    assert body.status_code == 200, body.text
    assert body.json()["max_seconds"] == 10
    assert _duration(client.get(_path(body.json()["url"])).content, tmp_path) == pytest.approx(10, abs=0.6)
    assert client.get(f"/v1/assets/{asset_id}/preview", params=params).status_code == 200
    assert client.get(f"/v1/assets/{asset_id}", params=params).status_code == 200
    # Only the project's own clips.
    assert client.get(f"/v1/assets/{outsider}/playback", params=params).status_code == 403
    assert client.get(f"/v1/assets/{outsider}/preview", params=params).status_code == 403
    assert client.get(f"/v1/assets/{outsider}", params=params).status_code == 403


@pytest.mark.fast
def test_every_link_is_new_even_within_a_second(monkeypatch):
    # The player renews a failed link; an identical one wouldn't reload.
    monkeypatch.setenv("JWT_SECRET", "x")
    get_settings.cache_clear()
    from src.server.api.routers.playback import mint_stream_token, read_stream_token

    a, b = mint_stream_token("ten_1", "ast_1"), mint_stream_token("ten_1", "ast_1")
    assert a != b
    assert read_stream_token(a)["a"] == read_stream_token(b)["a"] == "ast_1"


@pytest.mark.slow
def test_the_cap_goes_by_the_file_not_a_stored_duration(env, media, tmp_path):
    # A wrong duration on the asset (client-supplied, or before the probe)
    # mustn't let a public page play the whole video.
    client, admin, library_id, *_ = env
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 36)).save(buf, format="JPEG")
    buf.seek(0)
    data = {"library_id": library_id, "rel_path": "lies.mov", "file_size": "1000", "media_type": "video",
            "width": "64", "height": "36", "exif": json.dumps({"sha256": os.urandom(32).hex(), "duration_sec": 2}),
            "lineage": ingest_made()}
    r = client.post("/v1/ingest", data=data, files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=admin)
    assert r.status_code == 200, r.text
    asset_id = r.json()["asset_id"]
    _upload(env, asset_id, "analysis_proxy", media["long"])
    params = _public(env)
    url = _path(client.get(f"/v1/assets/{asset_id}/playback", params=params).json()["url"])
    assert _duration(client.get(url).content, tmp_path) == pytest.approx(10, abs=0.6)


@pytest.mark.slow
def test_if_range_with_an_old_version_gets_the_whole_new_file(env, media):
    client, admin, *_ = env
    url = _path(_playback(env, _video(env, media, "ifrange")).json()["url"])
    etag = client.get(url).headers["etag"]
    same = client.get(url, headers={"Range": "bytes=0-99", "If-Range": etag})
    assert same.status_code == 206 and len(same.content) == 100
    stale = client.get(url, headers={"Range": "bytes=0-99", "If-Range": '"something-else"'})
    assert stale.status_code == 200 and stale.content == media["proxy"]


@pytest.mark.fast
def test_an_unreadable_public_setting_stays_capped():
    from unittest.mock import MagicMock

    from src.server.tenant_settings import (
        PUBLIC_DEFAULT_SECONDS,
        get_public_video_preview_max_seconds,
    )

    session = MagicMock()
    session.get.return_value = MagicMock(value="garbage")
    assert get_public_video_preview_max_seconds(session) == PUBLIC_DEFAULT_SECONDS


@pytest.mark.fast
def test_clearing_cuts_for_deleted_assets_leaves_the_rest(tmp_path, monkeypatch):
    from src.server.api.routers import playback

    folder = tmp_path / "ten_1" / "playback"
    folder.mkdir(parents=True)
    a, b = "ast_01M4CWVZ2MPTAV6FSGG6B6VXVP", "ast_01M4CWVZYN681BTVTSA2CYJQNZ"
    for n in (f"{a}_preview_1-2_5s.mp4", f".{a}_tmp.mp4", f"{b}_analysis_proxy_x_5s.mp4"):
        (folder / n).write_bytes(b"x")
    monkeypatch.setattr(playback, "get_storage", lambda: LocalStorage(str(tmp_path)))
    playback.clear_cuts("ten_1", [a])
    assert [p.name for p in folder.iterdir()] == [f"{b}_analysis_proxy_x_5s.mp4"]


@pytest.fixture(scope="module")
def gps_preview(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("gps") / "preview.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=30:duration=3", "-f", "lavfi", "-i", "sine=duration=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-metadata", "location=+40.7000-074.0000/", "-movflags", "+faststart", str(out)], check=True)
    return out.read_bytes()


def _has_location(body: bytes, tmp_path: Path) -> bool:
    f = tmp_path / "probe.mp4"
    f.write_bytes(body)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags:stream_tags", "-of", "json",
                          str(f)], capture_output=True, text=True, check=True).stdout
    return "location" in out.lower()


@pytest.mark.slow
@pytest.mark.parametrize("public_cap", [10, 2, None], ids=["default", "short", "lifted"])
def test_public_pages_never_serve_where_a_clip_was_shot(env, gps_preview, tmp_path, public_cap):
    # Previews made before the CLI stripped metadata still carry GPS.
    client, admin, *_ = env
    asset_id = _ingest(env, f"gps_{public_cap}.mov")
    _upload(env, asset_id, "video_preview", gps_preview)
    assert _has_location(gps_preview, tmp_path)
    client.patch("/v1/tenant/settings", json={"public_video_preview_max_seconds": public_cap}, headers=admin)
    params = _public(env)
    for body in (
        client.get(_path(client.get(f"/v1/assets/{asset_id}/playback", params=params).json()["url"])).content,
        client.get(f"/v1/assets/{asset_id}/preview", params=params).content,
        client.get(f"/v1/assets/{asset_id}/artifacts/video_preview", params=params).content,
    ):
        assert body[:12].find(b"ftyp") != -1
        assert not _has_location(body, tmp_path)
