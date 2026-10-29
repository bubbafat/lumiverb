"""A video's scenes, found from its analysis copy (src/processing/video/scene_segmenter.py,
run by work.py), merged where they look alike once the whole video is found
(the server, as the last chunk completes), then described one by one
(scene_vision). People see the two as one: Scene descriptions, with one
setting, Detail (src/producers/scenes/detail.py). Found again, a clip's old
scenes go with their descriptions (POST /v1/video/{id}/chunks with redo)."""

from src.producers import names
from src.producers.contract import VIDEO, Data, Field, ProducerSpec, Setting
from src.producers.pools import SCENES
from src.producers.scenes.detail import DEFAULT, DETAILS

# Version 5: pictures compared by an 8 × 8 wavelet hash, not a 16 × 16
# pHash, which merged 1 of 333 neighbouring scenes that were the same scene
# and cut 93 times, never at a real change (60 of Robert's videos, Oct 28:
# detail.py). Found again, a scene whose picture is a frame already
# described keeps that description (VideoSceneRepository.keep_descriptions),
# so a redo describes only new frames. 4: found again on analysis proxies
# with a keyframe every 2 s (analysis_proxy version 2): those found on older proxies cut at their
# sparser keyframes (Robert, Oct 14: redo them, after all new work; a
# redo's tier is the last, src/server/scheduler/kinds.py). 3: scenes by
# Detail (windows scaled to the video's length, cuts within them, alike
# neighbours merged). 2: scenes at the video's own time. 1 timed each 30 s
# chunk from where it was scanned, and scanned every chunk to the clip's end.
MAKES = Data(
    names.SCENES, "Where its scenes start and end, each with its picture.",
    (Field("start_ms", "number"), Field("end_ms", "number"), Field("picture", "image")),
    stored=("video_scenes", "assets.video_indexed"), many=True,
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.ANALYSIS_PROXY, names.METADATA),
    artifact=names.SCENES, version="5", media=VIDEO, unit="second", title="Scenes", order=50,
    # The probe's duration first.
    applies="a.media_type = 'video' AND a.duration_sec IS NOT NULL",
    made="a.video_indexed",
    settings=(
        # A level, not in the hash: scenes made at a lower detail than now are
        # "below" (counted, made again when asked); at it or above, current.
        Setting("detail", DEFAULT, "Detail", kind="level", choices=DETAILS, remakes=False),
    ),
    # Found again, a clip's scenes are new ones: their descriptions go with the
    # old, but for a scene on a frame already described (keep_descriptions).
    redo_also=(names.SCENE_DESCRIPTIONS,),
    redo_on_source_change=False,
    part_of=names.SCENE_DESCRIPTIONS,
    run="src.producers.scenes.work:Scenes", pool=SCENES,
)
