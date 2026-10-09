"""A video's scenes, found from its analysis copy (src/client/video/scene_segmenter.py,
run by src/client/cli/video_index.py)."""

from src.producers.contract import NOT_READ_YET, VIDEO, ProducerSpec, Setting

PRODUCER = ProducerSpec(
    artifact="scenes", producer="scene-detect", version="1", media=VIDEO, unit="second", title="Scenes", order=50,
    # The probe's duration first.
    applies="a.media_type = 'video' AND a.duration_sec IS NOT NULL",
    made="a.video_indexed",
    settings=(
        Setting("frame_width", 480, "Frame width", unit="px", fixed=NOT_READ_YET),
        Setting("frames", "keyframes", "Frames looked at", kind="text", fixed=NOT_READ_YET),
        Setting("phash_threshold", 51, "Change threshold", advanced=True, fixed=NOT_READ_YET),
        Setting("phash_hash_size", 16, "Hash size", advanced=True, fixed=NOT_READ_YET),
        Setting("temporal_ceiling_sec", 30.0, "Longest scene", kind="float", unit="s", fixed=NOT_READ_YET),
        Setting("debounce_sec", 3.0, "Shortest scene", kind="float", unit="s", advanced=True, fixed=NOT_READ_YET),
    ),
    needs=("analysis_proxy",),
    cant_redo="Finding a video's scenes again means deleting the ones it has first; that isn't built yet.",
    redo_on_source_change=False,
    kind="scenes", flag="missing_video_scenes", run="src.server.scheduler.runners:scenes", pool="scenes",
)
