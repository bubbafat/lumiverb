"""A video's scenes, found from its analysis copy (src/processing/video/scene_segmenter.py,
run by work.py). Found again, a clip's old scenes go with their
descriptions (POST /v1/video/{id}/chunks with redo)."""

from src.producers.contract import VIDEO, ProducerSpec, Setting
from src.producers.pools import SCENES

PRODUCER = ProducerSpec(
    artifact="scenes", producer="scene-detect", version="1", media=VIDEO, unit="second", title="Scenes", order=50,
    # The probe's duration first.
    applies="a.media_type = 'video' AND a.duration_sec IS NOT NULL",
    made="a.video_indexed",
    settings=(
        Setting("frame_width", 480, "Frame width", minimum=160, maximum=1920, unit="px", advanced=True),
        Setting("frames", "keyframes", "Frames looked at", kind="text",
                fixed="Keyframes are the only frames it looks at."),
        # Bits of the 16 × 16 hash that must differ from the scene's first frame.
        Setting("phash_threshold", 51, "Change needed for a new scene", minimum=1, maximum=255, unit="of 256"),
        Setting("phash_hash_size", 16, "Hash size", advanced=True,
                fixed="The change needed counts its bits, so it stays 16 × 16."),
        # Videos are looked at 30 seconds at a time (CHUNK_DURATION_SEC), and each
        # chunk closes its last scene: a scene is never longer, so the longest
        # goes to 30 s. The shortest stays under the least of it, or a change
        # could never start a scene.
        Setting("temporal_ceiling_sec", 30.0, "Longest scene", kind="float", minimum=10, maximum=30, unit="s"),
        Setting("debounce_sec", 3.0, "Shortest scene", kind="float", minimum=0, maximum=9, unit="s",
                advanced=True),
    ),
    needs=("analysis_proxy",),
    # Found again, a clip's scenes are new ones: their descriptions go with the old.
    redo_also=("scene_vision",),
    redo_on_source_change=False,
    kind="scenes", flag="missing_video_scenes", run="src.producers.scenes.work:Scenes", pool=SCENES,
)
