"""The GPU models the scheduler holds, shared by every account (ADR-016 phase 4).

CLIP is loaded once per model and weights, whichever account asks; the face
process is one for the scheduler, kept between jobs, replaced when it dies
or hangs, and let go of when unused. Nothing in either is an account's: each
account's jobs save through their own client.
"""

from __future__ import annotations

import multiprocessing as mp
import threading
from unittest.mock import MagicMock

import pytest

from src.producers import runner
from src.producers.faces.detect import DETECT_TIMEOUT_SEC, FaceDetector
from src.server.scheduler.models import IDLE_SEC, Models

pytestmark = pytest.mark.fast


def test_one_clip_model_serves_every_account_and_a_new_one_loads_only_when_its_settings_change() -> None:
    made: list[tuple[str, str]] = []
    models = Models(clip_factory=lambda m, p: made.append((m, p)) or object())
    first = models.clip("ViT-B-32", "openai")
    assert models.clip("ViT-B-32", "openai") is first  # another account's job: the same model
    assert made == [("ViT-B-32", "openai")]
    models.clip("ViT-L-14", "openai")
    assert made == [("ViT-B-32", "openai"), ("ViT-L-14", "openai")]


def test_one_face_process_serves_every_account() -> None:
    made: list[int] = []
    models = Models(face_factory=lambda: made.append(1) or MagicMock(idle=True))
    assert models.faces() is models.faces()
    assert made == [1]


def test_models_unused_for_a_while_are_let_go_of() -> None:
    now = [0.0]
    faces = MagicMock(idle=True)
    models = Models(clip_factory=lambda m, p: object(), face_factory=lambda: faces, clock=lambda: now[0])
    clip = models.clip("ViT-B-32", "openai")
    models.faces()
    now[0] = IDLE_SEC - 1
    models.let_go_of_idle()
    faces.close_if_idle.assert_not_called()
    now[0] = IDLE_SEC + 1
    models.let_go_of_idle()
    faces.close_if_idle.assert_called_once()
    assert models.clip("ViT-B-32", "openai") is not clip  # loaded again when next asked for


def test_letting_go_of_the_face_process_is_said_once(caplog) -> None:
    # Review round 2: every look after the first said it again (1 a second).
    now = [0.0]
    detector = FaceDetector(500, pool_factory=MagicMock)
    detector._pool = MagicMock()  # a job ran
    models = Models(face_factory=lambda: detector, clock=lambda: now[0])
    models.faces()
    caplog.set_level("INFO", logger="src.server.scheduler.models")
    for _ in range(5):
        now[0] += IDLE_SEC + 1
        models.let_go_of_idle()
    assert sum("face detection process" in r.getMessage() for r in caplog.records) == 1


def test_a_face_process_in_the_middle_of_a_call_isnt_let_go_of() -> None:
    now = [0.0]
    pool = MagicMock()
    detector = FaceDetector(500, pool_factory=lambda: pool)
    detector._pool, detector._running = pool, 1  # a photo under way
    models = Models(face_factory=lambda: detector, clock=lambda: now[0])
    models.faces()
    now[0] += IDLE_SEC + 1
    models.let_go_of_idle()
    pool.terminate.assert_not_called()


def test_a_face_model_that_wont_load_is_the_machines_never_a_photos(monkeypatch) -> None:
    """Review: a missing extra or a CUDA mismatch charged every photo."""
    import io

    from PIL import Image

    from src.producers.faces import detect

    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="JPEG")
    monkeypatch.setattr(detect, "_provider", MagicMock(side_effect=RuntimeError("CUDA driver mismatch")))
    with pytest.raises(RuntimeError):  # the parent sees the call fail: Died, uncharged
        detect.detect_in_child(buf.getvalue(), {})


def test_clip_loads_without_holding_up_the_dispatching_thread() -> None:
    import threading

    loading, release = threading.Event(), threading.Event()

    def slow(model, pretrained):
        loading.set()
        release.wait(5)
        return object()

    models = Models(clip_factory=slow)
    t = threading.Thread(target=models.clip, args=("ViT-B-32", "openai"), daemon=True)
    t.start()
    assert loading.wait(5)
    done = threading.Event()
    threading.Thread(target=lambda: (models.let_go_of_idle(), done.set()), daemon=True).start()
    assert done.wait(1)  # the tick's look isn't held up by the load
    release.set()
    t.join(5)


def test_the_face_process_does_so_many_jobs_photos_before_a_fresh_one() -> None:
    from src.shared.producers import PRODUCERS

    models = Models(face_batches_per_process=4)
    assert models.faces()._per_process == 4 * PRODUCERS["faces"].batch


# ---------------------------------------------------------------------------
# The face process
# ---------------------------------------------------------------------------


def test_the_face_process_is_kept_between_calls() -> None:
    pool = MagicMock()
    pool.apply_async.return_value.get.return_value = {"faces": []}
    made: list[int] = []
    detector = FaceDetector(500, pool_factory=lambda: made.append(1) or pool)
    assert detector.detect(b"a", {}) == {"faces": []}
    detector.detect(b"b", {"det_size": 640})
    assert made == [1]
    assert pool.apply_async.call_args.args[1] == (b"b", {"det_size": 640})


def test_a_face_process_that_hangs_is_given_up_and_replaced() -> None:
    dead, fresh = MagicMock(), MagicMock()
    # A process killed mid-photo never answers: the wait times out.
    dead.apply_async.return_value.get.side_effect = mp.TimeoutError()
    fresh.apply_async.return_value.get.return_value = {"faces": []}
    pools = iter([dead, fresh])
    now = [0.0]

    def clock() -> float:
        now[0] += 30.0  # each look at the clock, 30 s on
        return now[0]

    detector = FaceDetector(500, pool_factory=lambda: next(pools), clock=clock)
    with pytest.raises(runner.Died):  # a crash: counted, uncharged
        detector.detect(b"a", {})
    assert detector.detect(b"b", {}) == {"faces": []}
    dead.terminate.assert_called_once()
    assert 9 <= dead.apply_async.return_value.get.call_count <= DETECT_TIMEOUT_SEC / 30 + 1


def test_a_face_process_that_dies_is_replaced() -> None:
    dead, fresh = MagicMock(), MagicMock()
    dead.apply_async.return_value.get.side_effect = RuntimeError("segfault")
    fresh.apply_async.return_value.get.return_value = {"faces": []}
    pools = iter([dead, fresh])
    detector = FaceDetector(500, pool_factory=lambda: next(pools))
    with pytest.raises(runner.Died):
        detector.detect(b"a", {})
    assert detector.detect(b"b", {}) == {"faces": []}


def test_a_call_ends_as_soon_as_its_process_is_let_go_of() -> None:
    # Review round 2: a stop, or the process let go of, left the GPU slot
    # (CLIP and faces, every account) waiting up to 15 minutes.
    pool = MagicMock()
    pool.apply_async.return_value.get.side_effect = mp.TimeoutError()
    detector = FaceDetector(500, pool_factory=lambda: pool, poll_sec=0.01, clock=lambda: 0.0)
    out: list = []

    def call() -> None:
        try:
            detector.detect(b"a", {})
        except runner.Stopped:
            out.append("let go")

    t = threading.Thread(target=call, daemon=True)
    t.start()
    for _ in range(500):
        if pool.apply_async.called:
            break
        threading.Event().wait(0.01)
    assert not detector.idle
    assert detector.close() is True
    t.join(2)
    assert out == ["let go"] and detector.idle
    assert detector.close() is False  # nothing left to let go of


def test_a_process_let_go_of_before_its_call_starts_is_not_a_death() -> None:
    # Review round 3: "Pool not running" read as a dead process, held an hour.
    pool = MagicMock()
    detector = FaceDetector(500, pool_factory=lambda: pool)

    def let_go(*a, **kw):
        detector.close()
        raise ValueError("Pool not running")

    pool.apply_async.side_effect = let_go
    with pytest.raises(runner.Stopped):
        detector.detect(b"a", {})


def test_the_child_says_whats_wrong_with_an_image_rather_than_dying() -> None:
    from src.producers.faces.detect import detect_in_child

    out = detect_in_child(b"not an image", {})
    assert "error" in out
