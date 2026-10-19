"""Scene descriptions, by the vision machines (Settings → AI), one per scene."""

from src.producers import names
from src.producers.contract import VIDEO, Data, Field, ProducerSpec
from src.producers.pools import VISION
from src.producers.prompts import VISION_PROMPT, ai_settings

# The scheduler describes scenes with the vision machines and these settings, which aren't offered yet.
NOT_OFFERED = "Scenes are described with these settings; changing them isn't offered yet."

MAKES = Data(
    names.SCENE_DESCRIPTIONS, "Each scene described, with tags.",
    (Field("start_ms", "number"), Field("end_ms", "number"), Field("description", "text"),
     Field("tags", "list", (Field("tag", "text"),))),
    stored=("video_scenes",), many=True,
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.ANALYSIS_PROXY, names.SCENES),
    artifact="scene_vision", producer="scene-vision", version="1", media=VIDEO, unit="second", title="Scene descriptions",
    order=60,
    # The scenes shown, and a redo's once found (described, then swapped in);
    # not a redo's still being found.
    applies=("a.media_type = 'video' AND a.video_indexed"
             " AND EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id"
             " AND s.staged IS DISTINCT FROM 'finding')"),
    made=("NOT EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id AND s.description IS NULL"
          " AND s.staged IS DISTINCT FROM 'finding')"),
    settings=ai_settings(VISION_PROMPT, fixed=NOT_OFFERED),
    redo_on_source_change=False,
    kind="scene_vision", flag="missing_scene_vision", run="src.producers.scene_vision.work:SceneVision",
    pool=VISION,
)
