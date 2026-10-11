"""Photos' face levels with the real detector (InsightFace buffalo_l), on the
repo's face fixtures, as the face process uses them (FaceSettings from the
level's gates). Opt-in: pytest -m ai.

Measured Oct 11 (CoreML and CPU alike): the crowd gives 0 faces at Low, 2 at
Normal and about 11 at High; the portrait's face (confidence 0.83, 299 px
wide) passes every level. Assertions keep wide margins: Vision-style
near-the-gate counts aren't asserted (CLAUDE.md, Gotchas).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from src.processing.workers.faces.insightface_provider import FaceSettings, InsightFaceProvider
from src.producers.faces.levels import gates

pytestmark = pytest.mark.ai

FIXTURES = Path(__file__).resolve().parents[1] / "clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/Fixtures"


@pytest.fixture(scope="module")
def provider():
    if not (Path.home() / ".insightface/models/buffalo_l").is_dir():
        pytest.skip("InsightFace's buffalo_l isn't downloaded")
    return InsightFaceProvider()


def _found(provider, name: str, detail: str) -> int:
    provider.settings = FaceSettings.for_producer(gates(detail))
    with Image.open(FIXTURES / f"{name}.jpg") as img:
        img = img.convert("RGB")
        img.thumbnail((1280, 1280))  # the proxy's size, as faces are found in proxies
        return len(provider.detect_faces(img))


def test_a_crowd_gives_more_faces_level_by_level(provider):
    low, normal, high = (_found(provider, "face_crowd", d) for d in ("low", "normal", "high"))
    assert low <= normal < high
    assert high >= normal + 5  # the small faces at the back


def test_a_clear_portrait_is_found_at_every_level(provider):
    assert [_found(provider, "face_single", d) for d in ("low", "normal", "high")] == [1, 1, 1]
