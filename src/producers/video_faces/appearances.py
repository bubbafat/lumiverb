"""A person seen across a video's frames, as one face. Pure (numpy).

The faces found in the frames looked at are grouped by who they look like
(their ArcFace embeddings), and each group is saved as one face: People
shows a video once per person, at the times they're seen, and a long video
doesn't fill the face groups with copies of one face.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# Cosine distance under which two faces in one video are one person: the
# gate a face found again is held to (FaceRepository.REDETECT_MAX_EMBEDDING_DISTANCE).
# The same person further apart (a profile and a full face) is two faces,
# which the face groups may join; two people closer than this would share
# a name, so it's no looser.
SAME_PERSON = 0.4


@dataclass
class Appearance:
    """One person in a video: the face shown (its box and confidence, in the
    frame at frame_ms), their embedding (the mean of their faces', unit
    length) and every time they were seen at, in order."""

    face: dict
    frame_ms: int
    embedding: list[float] | None
    seen_ms: list[int]
    _sum: Any = field(default=None, repr=False)
    _score: float = field(default=-1.0, repr=False)


def _score(face: dict) -> float:
    """Which of a person's faces is shown: the most confident, weighed by its size."""
    box = face.get("bounding_box") or {}
    return float(face.get("detection_confidence") or 0.0) * math.sqrt(max(box.get("w", 0.0) * box.get("h", 0.0), 0.0))


def _unit(values: Sequence[float]) -> Any:
    import numpy as np

    vec = np.asarray(values, dtype=np.float64)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


def appearances(found: Sequence[tuple[int, dict]]) -> list[Appearance]:
    """found: (frame time in ms, face) for every face found, a face being
    {bounding_box, detection_confidence, embedding}. Frame by frame, in time
    order, each face joins the person whose faces it's nearest (their mean),
    under SAME_PERSON; the nearest pairs first, and never two faces of one
    frame to one person (they're two people). A face left over, or without
    an embedding, is a person of its own. In the order first seen."""
    by_frame: dict[int, list[dict]] = {}
    for ms, face in found:
        by_frame.setdefault(int(ms), []).append(face)
    people: list[Appearance] = []
    for ms in sorted(by_frame):
        faces = by_frame[ms]
        vecs = [_unit(f["embedding"]) if f.get("embedding") else None for f in faces]
        pairs = []
        for fi, vec in enumerate(vecs):
            if vec is None:
                continue
            for pi, person in enumerate(people):
                if person._sum is None:
                    continue
                distance = 1.0 - float(vec @ _unit(person._sum))
                if distance < SAME_PERSON:
                    pairs.append((distance, fi, pi))
        joined: dict[int, int] = {}
        taken: set[int] = set()
        for _, fi, pi in sorted(pairs):
            if fi not in joined and pi not in taken:
                joined[fi] = pi
                taken.add(pi)
        for fi, face in enumerate(faces):
            if fi in joined:
                person = people[joined[fi]]
                person._sum = person._sum + vecs[fi]
                person.seen_ms.append(ms)
            else:
                person = Appearance(face=face, frame_ms=ms, embedding=None, seen_ms=[ms], _sum=vecs[fi])
                people.append(person)
            score = _score(face)
            if score > person._score:
                person.face, person.frame_ms, person._score = face, ms, score
    for person in people:
        person.embedding = _unit(person._sum).tolist() if person._sum is not None else None
    return people
