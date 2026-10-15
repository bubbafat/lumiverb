"""A video's faces, saved and shown (POST /v1/video/{id}/faces), on real Postgres.

The video faces producer (src/producers/video_faces) sends one face a person
seen, with the times they're seen and a crop cut from the frame: the
server keeps the times, serves that crop (never one cut from the poster),
keeps a named face through a redo, and a person's faces and the person:
filter bring the video.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from src.server.storage.local import get_storage
from src.shared.face_crop import face_crop
from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations
from tests.machine_lineage import made, made_json

pytestmark = pytest.mark.slow



def _model_available() -> bool:
    from src.processing.workers.faces.insightface_provider import model_available

    return model_available()

@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Control + tenant DB, storage in a folder of the test's. Yields (client, auth, library_id, tenant_engine)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = tmp_path_factory.mktemp("data")
    saved = os.environ.get("DATA_DIR")
    with PostgresContainer(PG_IMAGE) as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(make_url(control_url).set(database="{tenant_id}"))
        os.environ["ADMIN_KEY"] = "test-admin-video-faces"
        os.environ["DATA_DIR"] = str(data_dir)
        get_settings.cache_clear()
        get_storage.cache_clear()
        _engines.clear()

        with patch("src.server.accounts.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post("/v1/admin/tenants", json={"name": "VideoFaces", "plan": "free"},
                                headers={"Authorization": "Bearer test-admin-video-faces"})
                assert r.status_code == 200, r.text
                tenant_id, api_key = r.json()["tenant_id"], r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)
            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            tenant_engine = create_engine(tenant_url)
            with TestClient(app) as client:
                auth = {"Authorization": f"Bearer {api_key}"}
                library_id = client.post("/v1/libraries", json={"name": "Footage", "root_path": "/footage"},
                                         headers=auth).json()["library_id"]
                yield client, auth, library_id, tenant_engine
            tenant_engine.dispose()
    _engines.clear()
    if saved is None:
        os.environ.pop("DATA_DIR", None)
    else:
        os.environ["DATA_DIR"] = saved
    get_settings.cache_clear()
    get_storage.cache_clear()


def _clip(engine, library_id, rel_path: str, media_type: str = "video") -> str:
    with Session(engine) as session:
        return make_clip(session, library_id, rel_path, media_type=media_type)


def _unit(i: int, tilt: float = 0.0) -> list[float]:
    vec = [0.0] * 512
    vec[i] = 1.0
    vec[(i + 1) % 512] = tilt
    return vec


def _crop(color: tuple[int, int, int]) -> bytes:
    frame = Image.new("RGB", (640, 360), color)
    return face_crop(frame, {"x": 0.4, "y": 0.3, "w": 0.1, "h": 0.2})


PERSON_A = {"bounding_box": {"x": 0.1, "y": 0.2, "w": 0.15, "h": 0.25}, "detection_confidence": 0.95,
            "embedding": _unit(0), "frame_ms": 4000, "seen_ms": [1000, 4000, 9000]}
PERSON_B = {"bounding_box": {"x": 0.6, "y": 0.3, "w": 0.1, "h": 0.15}, "detection_confidence": 0.8,
            "embedding": _unit(1), "frame_ms": 9000, "seen_ms": [9000]}


def _send(client, auth, asset_id: str, faces: list[dict], crops: list[bytes] | None = None, *,
          lineage: str | None = None, **fields):
    crops = [_crop((200, 120, 90)) for _ in faces] if crops is None else crops
    return client.post(f"/v1/video/{asset_id}/faces", headers=auth, data={
        "faces": json.dumps(faces), "lineage": made_json("video_faces") if lineage is None else lineage, **fields,
    }, files=[("crops", (f"{i}.webp", c, "image/webp")) for i, c in enumerate(crops)])


def test_a_videos_people_are_saved_with_their_times_and_crops(env):
    client, auth, library_id, engine = env
    asset_id = _clip(engine, library_id, "faces/interview.mov")
    crops = [_crop((200, 120, 90)), _crop((30, 60, 200))]
    r = _send(client, auth, asset_id, [PERSON_A, PERSON_B], crops)
    assert r.status_code == 200, r.text
    assert r.json()["face_count"] == 2
    a_id, b_id = r.json()["face_ids"]
    assert client.get(f"/v1/assets/{asset_id}", headers=auth).json()["face_count"] == 2  # the people seen

    faces = {f["face_id"]: f for f in client.get(f"/v1/assets/{asset_id}/faces", headers=auth).json()["faces"]}
    assert faces[a_id]["frame_ms"] == 4000 and faces[a_id]["seen_ms"] == [1000, 4000, 9000]
    assert faces[b_id]["frame_ms"] == 9000 and faces[b_id]["seen_ms"] == [9000]
    # Each face's crop is the one cut from its frame, as sent.
    for face_id, crop in zip((a_id, b_id), crops, strict=True):
        got = client.get(f"/v1/faces/{face_id}/crop", headers=auth)
        assert got.status_code == 200 and got.content == crop
    with engine.connect() as conn:
        row = conn.execute(text("SELECT producer, level, outcome FROM artifact_lineage"
                                " WHERE asset_id = :a AND artifact = 'video_faces'"), {"a": asset_id}).one()
    assert tuple(row) == ("insightface-video", 1, "ok")  # Low


def test_a_video_with_no_one_in_it_is_done(env):
    client, auth, library_id, engine = env
    asset_id = _clip(engine, library_id, "faces/landscape.mov")
    r = _send(client, auth, asset_id, [])
    assert r.status_code == 200, r.text
    assert r.json() == {"face_count": 0, "face_ids": []}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT outcome FROM artifact_lineage WHERE asset_id = :a"
                                 " AND artifact = 'video_faces'"), {"a": asset_id}).scalar() == "empty"


def test_a_named_face_keeps_its_name_when_found_again(env):
    client, auth, library_id, engine = env
    asset_id = _clip(engine, library_id, "faces/again.mov")
    a_id, b_id = _send(client, auth, asset_id, [{**PERSON_A, "embedding": _unit(3)},
                                                {**PERSON_B, "embedding": _unit(5)}]).json()["face_ids"]
    person = client.post("/v1/people", json={"display_name": "Ada", "face_ids": [a_id]}, headers=auth)
    assert person.status_code in (200, 201), person.text
    person_id = person.json()["person_id"]

    # Looked at again (a higher Detail): the same person, seen at other times.
    again = {**PERSON_A, "embedding": _unit(3, tilt=0.1), "frame_ms": 2500, "seen_ms": [500, 2500, 6000, 9000]}
    new_crop = _crop((10, 200, 10))
    r = _send(client, auth, asset_id, [again], [new_crop])
    assert r.status_code == 200, r.text
    assert r.json()["face_ids"] == [a_id]  # the named face, found again
    faces = client.get(f"/v1/assets/{asset_id}/faces", headers=auth).json()["faces"]
    assert [f["face_id"] for f in faces] == [a_id]  # B wasn't found again, and nobody named it
    assert faces[0]["person"]["person_id"] == person_id
    assert faces[0]["frame_ms"] == 2500 and faces[0]["seen_ms"] == [500, 2500, 6000, 9000]
    assert client.get(f"/v1/faces/{a_id}/crop", headers=auth).content == new_crop


def test_a_persons_faces_and_the_person_filter_bring_the_video(env):
    client, auth, library_id, engine = env
    video = _clip(engine, library_id, "faces/party.mov")
    # Someone no other test names (a face like a named one is theirs at once).
    face_id = _send(client, auth, video, [{**PERSON_A, "embedding": _unit(7)}]).json()["face_ids"][0]
    person_id = client.post("/v1/people", json={"display_name": "Grace", "face_ids": [face_id]},
                            headers=auth).json()["person_id"]

    items = client.get(f"/v1/people/{person_id}/faces", headers=auth).json()["items"]
    assert [(i["asset_id"], i["media_type"], i["frame_ms"], i["seen_ms"]) for i in items] == [
        (video, "video", 4000, [1000, 4000, 9000])]
    found = client.get("/v1/query", params=[("f", f"library:{library_id}"), ("f", f"person:{person_id}")],
                       headers=auth)
    assert found.status_code == 200, found.text
    assert [i["asset_id"] for i in found.json()["items"]] == [video]


def test_a_video_faces_crop_is_never_cut_from_its_poster(env):
    client, auth, library_id, engine = env
    asset_id = _clip(engine, library_id, "faces/poster.mov")
    face_id = _send(client, auth, asset_id, [PERSON_A]).json()["face_ids"][0]
    # The video has a poster, and its face has lost its crop: a crop cut from
    # the poster would be of another moment, so there's none.
    storage = get_storage()
    buf = io.BytesIO()
    Image.new("RGB", (640, 360), (255, 255, 255)).save(buf, format="WEBP")
    storage.write("poster.webp", buf.getvalue())
    with engine.begin() as conn:
        conn.execute(text("UPDATE assets SET proxy_key = 'poster.webp' WHERE asset_id = :a"), {"a": asset_id})
        conn.execute(text("UPDATE faces SET crop_key = NULL WHERE face_id = :f"), {"f": face_id})
    assert client.get(f"/v1/faces/{face_id}/crop", headers=auth).status_code == 404


@pytest.mark.parametrize("case", ["photo", "crops", "not_webp", "no_lineage", "not_json", "other_producer", "no_times"])
def test_what_isnt_a_videos_faces_is_refused_before_anything_is_saved(env, case):
    client, auth, library_id, engine = env
    asset_id = _clip(engine, library_id, f"faces/refused-{case}.{'jpg' if case == 'photo' else 'mov'}",
                     "image" if case == "photo" else "video")
    status, code = {
        "photo": (400, "not_a_video"),
        "crops": (400, "crops_dont_match"),
        "not_webp": (400, "crop_not_valid"),
        "no_lineage": (422, "lineage_required"),
        "not_json": (400, "faces_not_valid"),
        "other_producer": (422, "lineage_wrong_producer"),
        "no_times": (400, "faces_not_valid"),
    }[case]
    if case == "crops":
        r = _send(client, auth, asset_id, [PERSON_A, PERSON_B], [_crop((1, 2, 3))])
    elif case == "not_webp":
        r = _send(client, auth, asset_id, [PERSON_A], [b"\x89PNG not a webp"])
    elif case == "no_lineage":
        r = _send(client, auth, asset_id, [PERSON_A], lineage="")
    elif case == "not_json":
        r = client.post(f"/v1/video/{asset_id}/faces", headers=auth,
                        data={"faces": "[{", "lineage": made_json("video_faces")})
    elif case == "other_producer":
        r = _send(client, auth, asset_id, [PERSON_A], lineage=json.dumps(made("faces")))
    elif case == "no_times":
        r = _send(client, auth, asset_id, [{**PERSON_A, "seen_ms": []}])
    else:
        r = _send(client, auth, asset_id, [PERSON_A])
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM faces WHERE asset_id = :a"), {"a": asset_id}).scalar() == 0
        assert conn.execute(text("SELECT face_count FROM assets WHERE asset_id = :a"), {"a": asset_id}).scalar() is None


# ---- end to end: the real detector, a real clip, the real server ------------

FIXTURES = Path(__file__).resolve().parents[1] / "clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/Fixtures"


class _Server:
    """The test server as the scheduler's API client: signed in, errors raised."""

    def __init__(self, client, auth) -> None:
        self._client, self._auth = client, auth

    def get(self, path: str, **kwargs):
        r = self._client.get(path, headers=self._auth, **kwargs)
        assert r.status_code == 200, r.text
        return r

    def post(self, path: str, **kwargs):
        r = self._client.post(path, headers=self._auth, **kwargs)
        assert r.status_code in (200, 201, 204), r.text
        return r


@pytest.mark.ai
def test_faces_in_a_real_clip_found_grouped_and_saved(env, tmp_path):
    """A clip of two shots, a portrait (0-3 s) then a group (3-6 s), looked
    at by InsightFace (buffalo_l) in its own process, as the scheduler does:
    the people come back with their times and crops cut from their frames."""
    import shutil
    import subprocess
    import threading

    from src.processing.producer_settings import ProducerSettings
    from src.producers.faces.detect import FaceDetector
    from src.producers.video_faces.work import VideoFaces

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg isn't installed")
    if not _model_available():
        pytest.skip("InsightFace's buffalo_l isn't downloaded")
    client, auth, library_id, engine = env
    clip = tmp_path / "two_shots.mp4"
    fit = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-loop", "1", "-t", "3", "-i", str(FIXTURES / "face_single.jpg"),
                    "-loop", "1", "-t", "3", "-i", str(FIXTURES / "face_group.jpg"),
                    "-filter_complex", f"[0]{fit}[a];[1]{fit}[b];[a][b]concat=n=2:v=1:a=0",
                    "-r", "25", "-pix_fmt", "yuv420p", str(clip)], check=True)
    asset_id = _clip(engine, library_id, "faces/two_shots.mp4")
    with engine.begin() as conn:
        for i, (start, end, rep) in enumerate([(0, 3000, 1500), (3000, 6000, 4500)]):
            conn.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                              " created_at) VALUES (:s, :a, :i, :start, :end, :rep, now())"),
                         {"s": f"scn_two_shots_{i}", "a": asset_id, "i": i, "start": start, "end": end, "rep": rep})

    server = _Server(client, auth)
    detector = FaceDetector(per_process=50)

    class _Acct:
        stopping = threading.Event()
        producers = ProducerSettings(None)

        class analysis_cache:  # noqa: N801 — the account's cache, holding this clip
            @staticmethod
            def get(_asset_id):
                return clip

        class models:  # noqa: N801
            @staticmethod
            def faces():
                return detector

    _Acct.client = server
    try:
        work = VideoFaces(_Acct, None)
        item = {"asset_id": asset_id, "library_id": library_id, "rel_path": "faces/two_shots.mp4", "duration_sec": 6.0}
        found = work.make(item)
        work.save(server, [(item, found)])
    finally:
        detector.close()

    assert found.frames == 2
    faces = client.get(f"/v1/assets/{asset_id}/faces", headers=auth).json()["faces"]
    # The portrait's person, and the group's people (two at least).
    assert any(1500 in f["seen_ms"] for f in faces), faces
    assert len([f for f in faces if 4500 in f["seen_ms"]]) >= 2, faces
    for f in faces:
        assert f["frame_ms"] in f["seen_ms"]
        crop = client.get(f"/v1/faces/{f['face_id']}/crop", headers=auth)
        assert crop.status_code == 200
        with Image.open(io.BytesIO(crop.content)) as img:
            assert img.format == "WEBP" and img.size == (128, 128)
    with engine.connect() as conn:
        dims = conn.execute(text("SELECT vector_dims(embedding_vector) FROM faces WHERE asset_id = :a"),
                            {"a": asset_id}).scalars().all()
    assert dims and set(dims) == {512}


TWO_PEOPLE = os.environ.get("LUMIVERB_TWO_PEOPLE_CLIP")


@pytest.mark.ai
@pytest.mark.skipif(not TWO_PEOPLE, reason="LUMIVERB_TWO_PEOPLE_CLIP names no clip (Robert's: two people, 8.7 s, 4K HEVC)")
def test_two_people_in_a_real_video_at_every_level(env, tmp_path):
    """Robert's clip of two people (not in the repo: its path comes from
    LUMIVERB_TWO_PEOPLE_CLIP), the whole way as the scheduler takes it: its
    analysis proxy rendered, its scenes found through the chunk routes,
    then its faces at Low, Normal and High with InsightFace in its own
    process, saved to the server. Each level finds the same two people, in
    every frame looked at, kept as the same two faces from level to level.
    Measured Oct 11 on a Mac (CoreML): 2 scenes; 2, 5 and 5 frames; the
    two about 1.0 apart (cosine distance)."""
    import shutil
    import subprocess
    import threading

    import numpy as np

    from src.processing.producer_settings import ProducerSettings
    from src.processing.video.analysis_proxy import render_analysis_proxy
    from src.producers.faces.detect import FaceDetector
    from src.producers.scenes.work import index_video_scenes
    from src.producers.video_faces.frames import frame_times
    from src.producers.video_faces.work import VideoFaces
    from src.shared.producers import level_of
    from tests.machine_lineage import made as made_lineage

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg isn't installed")
    if not _model_available():
        pytest.skip("InsightFace's buffalo_l isn't downloaded")
    client, auth, library_id, engine = env
    proxy = tmp_path / "analysis.mp4"
    render_analysis_proxy(Path(TWO_PEOPLE), proxy)
    duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(proxy)], capture_output=True, text=True, check=True).stdout)
    asset_id = _clip(engine, library_id, "faces/two_people.mov")
    server = _Server(client, auth)
    index_video_scenes(client=server, source_path=proxy, asset_id=asset_id, duration_sec=duration,
                       rel_path="faces/two_people.mov", lineage=made_lineage("scenes"), detail="normal")
    scenes = client.get(f"/v1/video/{asset_id}/scenes", headers=auth).json()["scenes"]
    assert scenes

    detector = FaceDetector(per_process=200)
    item = {"asset_id": asset_id, "library_id": library_id, "rel_path": "faces/two_people.mov",
            "duration_sec": duration}
    ids_before: set[str] | None = None
    try:
        for detail in ("low", "normal", "high"):
            class _Acct:
                stopping = threading.Event()
                producers = ProducerSettings(None)

                class analysis_cache:  # noqa: N801 — the account's cache, holding this clip's proxy
                    @staticmethod
                    def get(_asset_id):
                        return proxy

                class models:  # noqa: N801
                    @staticmethod
                    def faces():
                        return detector

            _Acct.client = server
            _Acct.producers._uses["video_faces"]["detail"] = detail
            work = VideoFaces(_Acct, None)
            found = work.make(item)
            work.save(server, [(item, found)])

            times = frame_times(scenes, round(duration * 1000), detail)
            faces = client.get(f"/v1/assets/{asset_id}/faces", headers=auth).json()["faces"]
            assert found.frames == len(times)
            assert len(faces) == 2, (detail, faces)
            for f in faces:  # each seen in every frame looked at
                assert f["seen_ms"] == times, (detail, f)
                crop = client.get(f"/v1/faces/{f['face_id']}/crop", headers=auth)
                assert crop.status_code == 200
            ids = {f["face_id"] for f in faces}
            assert ids_before is None or ids == ids_before, detail  # the same two, found again
            ids_before = ids
            a, b = (np.asarray(f["embedding"]) for f in found.faces)
            assert 1.0 - float(a @ b) > 0.6, detail  # two people, not one seen twice
            with engine.connect() as conn:
                count, level = conn.execute(text(
                    "SELECT a.face_count, l.level FROM assets a JOIN artifact_lineage l ON l.asset_id = a.asset_id"
                    " AND l.artifact = 'video_faces' WHERE a.asset_id = :a"), {"a": asset_id}).one()
            assert (count, level) == (2, level_of("video_faces", {"detail": detail})), detail
    finally:
        detector.close()
