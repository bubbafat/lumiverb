"""A video's scenes, found from its analysis copy (src/client/video/scene_segmenter.py,
run by src/client/cli/video_index.py)."""

from src.producers.contract import VIDEO, ProducerSpec

PRODUCER = ProducerSpec(
    artifact="scenes", producer="scene-detect", version="1", media=VIDEO, title="Scenes", order=50,
    # The probe's duration first.
    applies="a.media_type = 'video' AND a.duration_sec IS NOT NULL",
    made="a.video_indexed",
    defaults={"frame_width": 480, "frames": "keyframes", "phash_threshold": 51, "phash_hash_size": 16,
              "temporal_ceiling_sec": 30.0, "debounce_sec": 3.0},
    needs=("analysis_proxy",),
    cant_redo="Finding a video's scenes again means deleting the ones it has first; that isn't built yet.",
    redo_on_source_change=False,
    kind="scenes", flag="missing_video_scenes", run="src.server.scheduler.runners:scenes", pool="scenes",
)
