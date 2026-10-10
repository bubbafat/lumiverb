"""Scene descriptions, by the vision machines (Settings → AI), one per scene."""

from src.producers.contract import VIDEO, ProducerSpec
from src.producers.pools import VISION
from src.producers.prompts import VISION_PROMPT, ai_settings

# The scheduler describes scenes with the vision machines and these settings, which aren't offered yet.
NOT_OFFERED = "Scenes are described with these settings; changing them isn't offered yet."

PRODUCER = ProducerSpec(
    artifact="scene_vision", producer="scene-vision", version="1", media=VIDEO, unit="second", title="Scene descriptions",
    order=60,
    applies=("a.media_type = 'video' AND a.video_indexed"
             " AND EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id)"),
    made="NOT EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id AND s.description IS NULL)",
    settings=ai_settings(VISION_PROMPT, fixed=NOT_OFFERED), needs=("analysis_proxy",),
    redo_on_source_change=False,
    kind="scene_vision", flag="missing_scene_vision", run="src.producers.scene_vision.work:SceneVision",
    pool=VISION,
)
