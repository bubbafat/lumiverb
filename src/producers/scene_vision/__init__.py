"""Scene descriptions, by the vision machines (Settings → AI), one per scene."""

from src.producers import names
from src.producers.contract import VIDEO, Data, Field, ProducerSpec
from src.producers.pools import VISION
from src.producers.prompts import SCENE_PROMPT, ai_settings

# The scheduler describes scenes with the vision machines and these settings, which aren't offered yet (but the
# longest answer).
NOT_OFFERED = "Scenes are described with these settings; changing them isn't offered yet."

MAKES = Data(
    names.SCENE_DESCRIPTIONS, "Each scene described, with tags.",
    (Field("start_ms", "number"), Field("end_ms", "number"), Field("description", "text"),
     Field("tags", "list", (Field("tag", "text"),))),
    stored=("video_scenes",), many=True,
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.ANALYSIS_PROXY, names.SCENES),
    artifact=names.SCENE_DESCRIPTIONS, version="1", media=VIDEO, unit="second", title="Scene descriptions",
    order=60,
    # The scenes shown, and a redo's once found (described, then swapped in);
    # not a redo's still being found.
    applies=("a.media_type = 'video' AND a.video_indexed"
             " AND EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id"
             " AND s.staged IS DISTINCT FROM 'finding')"),
    made=("NOT EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id AND s.description IS NULL"
          " AND s.staged IS DISTINCT FROM 'finding')"),
    # A frame of a video, not a photo: its own prompt (SCENE_PROMPT). Its longest answer is offered, its default
    # the same (Robert, Oct 29): a model that thinks first (Claude Haiku 5.5) spends 500 tokens thinking.
    settings=ai_settings(SCENE_PROMPT, fixed=NOT_OFFERED, room_fixed=False),
    redo_on_source_change=False,
    # The video's own description is written from them.
    redo_also=(names.VIDEO_DESCRIPTION,),
    run="src.producers.scene_vision.work:SceneVision",
    pool=VISION,
)
