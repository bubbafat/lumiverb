"""A video's scenes, found from its analysis copy (src/processing/video/scene_segmenter.py,
run by work.py), merged where they look alike once the whole video is found
(the server, as the last chunk completes), then described one by one
(scene_vision). People see the two as one: Scene descriptions, with one
setting, Detail (src/producers/scenes/detail.py). Found again, a clip's old
scenes go with their descriptions (POST /v1/video/{id}/chunks with redo)."""

from src.producers.contract import VIDEO, ProducerSpec, Setting
from src.producers.pools import SCENES
from src.producers.scenes.detail import DEFAULT, DETAILS

# Version 3: scenes by Detail (windows scaled to the video's length, cuts
# within them, alike neighbours merged). 2: scenes at the video's own time.
# 1 timed each 30 s chunk from where it was scanned, and scanned every chunk
# to the clip's end.
PRODUCER = ProducerSpec(
    artifact="scenes", producer="scene-detect", version="3", media=VIDEO, unit="second", title="Scenes", order=50,
    # The probe's duration first.
    applies="a.media_type = 'video' AND a.duration_sec IS NOT NULL",
    made="a.video_indexed",
    settings=(
        # A level, not in the hash: scenes made at a lower detail than now are
        # "below" (counted, made again when asked); at it or above, current.
        Setting("detail", DEFAULT, "Detail", kind="level", choices=DETAILS, remakes=False),
    ),
    needs=("analysis_proxy",),
    # Found again, a clip's scenes are new ones: their descriptions go with the old.
    redo_also=("scene_vision",),
    redo_on_source_change=False,
    part_of="scene_vision",
    kind="scenes", flag="missing_video_scenes", run="src.producers.scenes.work:Scenes", pool=SCENES,
)
