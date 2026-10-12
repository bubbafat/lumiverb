"""Whether what a video's playback serves is the whole clip
(src/server/api/routers/playback.py whole_clip): the analysis proxy is; a
preview, the clip's first seconds (the scan's, video_preview's length), is
for a clip no longer than that. The API's answer: tests/test_video_playback_api.py."""

from __future__ import annotations

import pytest

from src.server.api.routers.playback import whole_clip
from src.shared.producers import PRODUCERS

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("source,duration,whole", [
    ("analysis_proxy", 3600.0, True),
    ("analysis_proxy", None, True),
    ("preview", 1.0, True),  # Robert's 1-second clip
    ("preview", 8.0, True),
    ("preview", 10.05, True),  # a frame or two over: rounding
    ("preview", 10.5, False),
    ("preview", None, False),  # not probed yet: can't say
])
def test_whole_clip(source, duration, whole):
    assert whole_clip(source, duration) is whole


def test_a_preview_is_the_scans_length():
    assert PRODUCERS["video_preview"].defaults["seconds"] == 10
