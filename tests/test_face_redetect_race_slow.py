# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A person's word on a face, written while re-detection of its clip is in
flight, is waited for and kept, never deleted with the face; and the two
never deadlock (one lock order: the clip, then its faces in face_id order)."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from sqlalchemy import text

from src.server.models.tenant import FacePersonMatch, Person
from src.server.repository.tenant import FaceRepository, PersonRepository
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha
from tests.test_reconciler import _library

BOX = {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}


def _redetect_while(env, clip: str, faces: list[dict], act) -> dict:
    """Re-detect the clip's faces; once the job has read what's there, a
    person does act(session) in their own transaction. Returns what came of
    the person's request: {"done": what act returned} or {"error": exc}."""
    outcome: dict = {}

    def person() -> None:
        try:
            with _db(env) as other:
                outcome["done"] = act(other)
        except Exception as exc:  # noqa: BLE001 — the test reads it
            outcome["error"] = exc

    thread = threading.Thread(target=person)
    pair = FaceRepository._pair_redetected_faces.__func__

    def pair_while_a_person_acts(cls, *args, **kwargs):
        thread.start()
        thread.join(timeout=2)  # done by now unless the job's locks make it wait
        return pair(cls, *args, **kwargs)

    with _db(env) as s, patch.object(FaceRepository, "_pair_redetected_faces", classmethod(pair_while_a_person_acts)):
        FaceRepository(s).submit_faces(clip, "m", "1", faces)
    thread.join(timeout=30)
    assert not thread.is_alive()
    return outcome


@pytest.mark.slow
def test_a_name_given_during_redetection_is_never_lost(env):
    lib = _library(env, "FaceRaceName")
    clip = _ingest_with(lib, "f.jpg", _sha(), None)
    with _db(env) as s:
        [face_id] = FaceRepository(s).submit_faces(clip, "m", "1", [{"bounding_box": BOX, "detection_confidence": 0.9}])

    # Re-detection doesn't find the face this time; meanwhile a person names it.
    outcome = _redetect_while(env, clip, [], lambda s: PersonRepository(s).create("Alice", face_ids=[face_id]))

    with _db(env) as s:
        named = s.execute(text("SELECT count(*) FROM face_person_matches WHERE face_id = :f AND confirmed"),
                          {"f": face_id}).scalar()
    # The name either stands, or the request saying it failed (the face was
    # gone by then): never a name accepted and then deleted.
    if "done" in outcome:
        assert named == 1, "a name the person was told was saved was deleted by re-detection"
    else:
        assert "error" in outcome


@pytest.mark.slow
def test_un_assigning_during_redetection_neither_deadlocks_nor_is_lost(env):
    lib = _library(env, "FaceRaceUnassign")
    clip = _ingest_with(lib, "g.jpg", _sha(), None)
    with _db(env) as s:
        [face_id] = FaceRepository(s).submit_faces(clip, "m", "1", [{"bounding_box": BOX, "detection_confidence": 0.9}])
        person_id = f"person_race_{_sha()[:8]}"
        s.add(Person(person_id=person_id, display_name="Bob"))
        s.flush()
        s.add(FacePersonMatch(match_id=f"fpm_{_sha()[:12]}", face_id=face_id, person_id=person_id, confirmed=False))
        s.commit()

    # Re-detection finds the face again; meanwhile a person says it isn't Bob.
    outcome = _redetect_while(env, clip, [{"bounding_box": BOX, "detection_confidence": 0.9}],
                              lambda s: PersonRepository(s).unassign_face(face_id))

    assert "done" in outcome, outcome  # no deadlock, no error
    with _db(env) as s:
        assert s.execute(text("SELECT count(*) FROM face_person_matches WHERE face_id = :f"),
                         {"f": face_id}).scalar() == 0
        # Un-assigned and remembered; or the job had dropped the machine's
        # match first and the person is told there was none to remove.
        assert outcome["done"] is False or s.execute(text("SELECT count(*) FROM face_person_rejections WHERE face_id = :f AND person_id = :p"),
                         {"f": face_id, "p": person_id}).scalar() == 1
