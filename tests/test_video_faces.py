"""Faces in videos (src/producers/video_faces): which frames are looked at,
how the faces found in them become the people seen, the crops, and the job
end to end on a real clip (ffmpeg makes its frames; the detector is a
stand-in that answers by what the frame shows). The real detector's run is
tests/test_video_faces_ai.py."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.processing.producer_settings import ProducerSettings
from src.producers.runner import Failed, Stopped, Waits
from src.producers.video_faces.appearances import SAME_PERSON, appearances
from src.producers.video_faces.frames import APART_MS, EVERY_SEC, frame_times
from src.producers.video_faces.work import VideoFaces
from src.shared.face_crop import SIZE, face_crop

pytestmark = pytest.mark.fast

SCENES = [{"scene_id": "s1", "start_ms": 0, "end_ms": 3000, "rep_frame_ms": 1500},
          {"scene_id": "s2", "start_ms": 3000, "end_ms": 6000, "rep_frame_ms": 4200}]


# ---- which frames ---------------------------------------------------------

def test_low_looks_at_each_scenes_picture():
    assert frame_times(SCENES, 6000, "low") == [1500, 4200]


def test_normal_looks_at_three_a_scene():
    middles = [{"start_ms": 0, "end_ms": 3000, "rep_frame_ms": 1500}, {"start_ms": 3000, "end_ms": 6000, "rep_frame_ms": 4500}]
    assert frame_times(middles, 6000, "normal") == [750, 1500, 2250, 3750, 4500, 5250]


def test_normal_leaves_out_a_frame_too_near_the_scenes_picture():
    scene = {"start_ms": 0, "end_ms": 4000, "rep_frame_ms": 1200}  # a quarter is 1000: the same picture
    assert frame_times([scene], 4000, "normal") == [1200, 3000]


def test_high_adds_a_frame_every_few_seconds_but_none_beside_a_picture():
    times = frame_times(SCENES, 12_000, "high")
    step = int(EVERY_SEC * 1000)
    assert {1500, 4200} <= set(times)
    assert 7500 in times and 10_500 in times  # the grid where no picture is
    assert 4500 not in times  # 300 ms from the second scene's picture
    assert times == sorted(times)
    assert all(b - a >= APART_MS for a, b in zip(times, times[1:], strict=False))
    assert len(times) == len({1500, 4200} | {t for t in range(step // 2, 12_000, step) if abs(t - 4200) >= APART_MS
                                             and abs(t - 1500) >= APART_MS})


def test_a_video_without_scenes_is_looked_at_in_its_middle():
    assert frame_times([], 8000, "low") == [4000]


def test_times_stay_inside_the_video_and_an_unknown_detail_is_low():
    late = [{"start_ms": 0, "end_ms": 9000, "rep_frame_ms": 9000}]
    assert frame_times(late, 6000, "sideways") == [5999]


# ---- the people seen ------------------------------------------------------

def _vec(*values: float) -> list[float]:
    return [*values, *([0.0] * (512 - len(values)))]


def _face(vec, conf=0.9, w=0.1, h=0.1):
    return {"bounding_box": {"x": 0.1, "y": 0.1, "w": w, "h": h}, "detection_confidence": conf, "embedding": vec}


def test_one_person_across_frames_is_one_face_seen_at_each_time():
    a1, a2 = _face(_vec(1.0, 0.1)), _face(_vec(1.0, -0.1), w=0.3, h=0.3)
    people = appearances([(4000, a2), (1000, a1), (7000, _face(_vec(1.0, 0.0)))])
    assert len(people) == 1
    assert people[0].seen_ms == [1000, 4000, 7000]
    assert people[0].face is a2 and people[0].frame_ms == 4000  # the largest of the confident shown
    emb = people[0].embedding
    assert abs(sum(x * x for x in emb) - 1.0) < 1e-9 and emb[1] == pytest.approx(0.0, abs=1e-9)  # the mean, unit length


def test_two_faces_in_one_frame_are_two_people_however_alike():
    twins = appearances([(1000, _face(_vec(1.0, 0.0))), (1000, _face(_vec(1.0, 0.01)))])
    assert len(twins) == 2


def test_different_people_stay_apart_and_each_frame_joins_the_nearest():
    a, b = _vec(1.0, 0.0), _vec(0.0, 1.0)
    people = appearances([(0, _face(a)), (0, _face(b)), (2000, _face(_vec(0.1, 1.0))), (2000, _face(_vec(1.0, 0.1)))])
    assert [p.seen_ms for p in people] == [[0, 2000], [0, 2000]]
    assert people[0].embedding[0] > 0.9 and people[1].embedding[1] > 0.9


def test_a_face_further_than_the_gate_is_someone_else():
    import math

    beyond = 1.0 - SAME_PERSON - 0.05  # cosine similarity just past the gate
    other = _vec(beyond, math.sqrt(1 - beyond * beyond))
    assert len(appearances([(0, _face(_vec(1.0))), (1000, _face(other))])) == 2


def test_a_face_without_an_embedding_is_someone_of_its_own():
    people = appearances([(0, _face(_vec(1.0))), (1000, _face(None)), (2000, _face(None))])
    assert [p.seen_ms for p in people] == [[0], [1000], [2000]]
    assert people[1].embedding is None


# ---- the crop -------------------------------------------------------------

def test_the_crop_is_the_face_square_with_room_around_it():
    frame = Image.new("RGB", (640, 360), (0, 0, 255))
    ImageDraw.Draw(frame).rectangle((256, 108, 320, 180), fill=(255, 0, 0))  # the face: x .4-.5, y .3-.5
    crop = face_crop(frame, {"x": 0.4, "y": 0.3, "w": 0.1, "h": 0.2})
    with Image.open(io.BytesIO(crop)) as img:
        assert img.format == "WEBP" and img.size == (SIZE, SIZE)
        r, g, b = img.convert("RGB").getpixel((SIZE // 2, SIZE // 2))
        assert r > 200 and b < 60
    assert face_crop(frame, {"x": 0.4, "y": 0.3, "w": 0.0, "h": 0.0}) is None


# ---- the job --------------------------------------------------------------

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg isn't installed")


@pytest.fixture(scope="module")
def red_then_blue(tmp_path_factory) -> Path:
    """Six seconds: red for three, then blue."""
    out = tmp_path_factory.mktemp("clip") / "red_then_blue.mp4"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=red:s=320x240:d=3:r=25",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=3:r=25",
                    "-filter_complex", "[0][1]concat=n=2:v=1:a=0", "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


class _Detector:
    """Answers by the frame's colour: red, Ada; blue, Ada (larger) and Bo."""

    def __init__(self, answer=None) -> None:
        self.answer = answer
        self.seen: list[str] = []

    def detect(self, image: bytes, settings: dict) -> dict:
        if self.answer is not None:
            return self.answer
        with Image.open(io.BytesIO(image)) as img:
            r, _, b = img.convert("RGB").resize((1, 1)).getpixel((0, 0))
        colour = "red" if r > b else "blue"
        self.seen.append(colour)
        faces = [_face(_vec(1.0), w=0.1, h=0.1)] if colour == "red" else [
            _face(_vec(1.0, 0.05), w=0.3, h=0.3), _face(_vec(0.0, 1.0), conf=0.8)]
        return {"detection_model": "insightface", "detection_model_version": "buffalo_l",
                "embedding_model": "buffalo_l", "faces": faces}


class _Client:
    def __init__(self, scenes) -> None:
        self.scenes = scenes
        self.posts: list[tuple[str, dict]] = []

    def get(self, path: str, **kwargs):
        assert path.endswith("/scenes")
        scenes = self.scenes

        class _R:
            def json(self):
                return {"scenes": scenes}
        return _R()

    def post(self, path: str, **kwargs):
        self.posts.append((path, kwargs))


class _Acct:
    def __init__(self, clip: Path | None, detector, scenes=SCENES, detail: str | None = None) -> None:
        self.client = _Client(scenes)
        self.stopping = threading.Event()
        self.producers = ProducerSettings(None)
        if detail:
            self.producers._uses["video_faces"]["detail"] = detail
        self._detector = detector
        outer = self

        class _Cache:
            def get(self, asset_id):
                return clip

        class _Models:
            def faces(self):
                return outer._detector

        self.analysis_cache = _Cache()
        self.models = _Models()


CLIP = {"asset_id": "ast_1", "library_id": "lib_1", "rel_path": "a/b.mp4", "duration_sec": 6.0, "content_hash": "sha256:" + "ab" * 32}


@needs_ffmpeg
def test_the_people_in_a_video_with_their_times_and_their_crops_from_their_frames(red_then_blue):
    detector = _Detector()
    acct = _Acct(red_then_blue, detector)
    work = VideoFaces(acct, None)
    found = work.make(CLIP)
    assert detector.seen == ["red", "blue"]  # each scene's picture, from the video itself
    assert found.frames == 2
    assert [(f["frame_ms"], f["seen_ms"]) for f in found.faces] == [(4200, [1500, 4200]), (4200, [4200])]
    assert len(found.crops) == 2
    with Image.open(io.BytesIO(found.crops[0])) as img:
        assert img.size == (SIZE, SIZE)
        r, _, b = img.convert("RGB").getpixel((SIZE // 2, SIZE // 2))
        assert b > r  # cut from the blue frame, where Ada is shown largest

    work.save(acct.client, [(CLIP, found)])
    [(path, kwargs)] = acct.client.posts
    assert path == "/v1/video/ast_1/faces"
    sent = json.loads(kwargs["data"]["faces"])
    assert [f["seen_ms"] for f in sent] == [[1500, 4200], [4200]]
    lineage = json.loads(kwargs["data"]["lineage"])
    assert lineage["producer"] == "insightface-video" and lineage["level"] == 1  # Low (Off is 0)
    assert lineage["source_hash"] == CLIP["content_hash"]
    assert [name for name, _ in kwargs["files"]] == ["crops", "crops"]
    assert kwargs["files"][0][1][1] == found.crops[0]


@needs_ffmpeg
def test_the_detail_now_says_which_frames_and_is_recorded(red_then_blue):
    detector = _Detector()
    acct = _Acct(red_then_blue, detector, detail="normal")
    work = VideoFaces(acct, None)
    found = work.make(CLIP)
    # 750, 1500, 2250; 4200, 5250 (3750 is the 4200 picture).
    assert found.frames == 5 and detector.seen == ["red", "red", "red", "blue", "blue"]
    work.save(acct.client, [(CLIP, found)])
    assert json.loads(acct.client.posts[0][1]["data"]["lineage"])["level"] == 2  # Normal


@needs_ffmpeg
def test_a_video_without_faces_is_saved_with_none(red_then_blue):
    acct = _Acct(red_then_blue, _Detector({"detection_model": "insightface", "detection_model_version": "buffalo_l",
                                           "embedding_model": "buffalo_l", "faces": []}))
    found = VideoFaces(acct, None).make(CLIP)
    assert found.frames == 2 and found.faces == [] and found.crops == []


def test_a_video_whose_frames_cant_be_read_is_its_failure():
    garbage = Path(__file__).parent / "fixtures" / "garbage.mp4"
    with pytest.raises(Failed, match="no frame could be looked at"):
        VideoFaces(_Acct(garbage, _Detector()), None).make(CLIP)


def test_without_its_analysis_proxy_it_waits():
    with pytest.raises(Waits, match="no analysis proxy"):
        VideoFaces(_Acct(None, _Detector()), None).make(CLIP)


@needs_ffmpeg
def test_the_gpu_out_of_memory_waits(red_then_blue):
    acct = _Acct(red_then_blue, _Detector({"error": "CUDA failure 2: out of memory"}))
    with pytest.raises(Waits):
        VideoFaces(acct, None).make(CLIP)


@needs_ffmpeg
def test_a_stop_stops_between_frames(red_then_blue):
    acct = _Acct(red_then_blue, _Detector())
    acct.stopping.set()
    with pytest.raises(Stopped):
        VideoFaces(acct, None).make(CLIP)
