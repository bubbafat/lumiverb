"""A video described as a whole, with tags, by the vision machines (Settings → AI): written from its scenes'
descriptions, in order, once every scene is described. Kept where a photo's description is, so the clip
shows it, search finds it and a person corrects it the same way."""

from src.producers import names
from src.producers.contract import VIDEO, Data, Field, ProducerSpec
from src.producers.pools import VISION
from src.producers.prompts import VIDEO_PROMPT, text_ai_settings

MAKES = Data(
    names.VIDEO_DESCRIPTION, "A video described as a whole, from its scenes in order, with tags.",
    (Field("description", "text"), Field("tags", "list", (Field("tag", "text"),))),
    stored=("asset_metadata",), person=("description", "tags"),
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.SCENE_DESCRIPTIONS,),
    artifact=names.VIDEO_DESCRIPTION, version="1", media=VIDEO, title="Video descriptions", order=65,
    # Its scenes shown, one described at least: it waits for the rest (it needs scene descriptions).
    applies=("a.media_type = 'video' AND EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id"
             " AND s.staged IS NULL AND s.description IS NOT NULL)"),
    made="EXISTS (SELECT 1 FROM asset_metadata am WHERE am.asset_id = a.asset_id)",
    # A gist, up to 5 details and the tags.
    settings=text_ai_settings(VIDEO_PROMPT, max_tokens=900),
    # Made from its scenes, not its file: their descriptions made again remake it (scene descriptions' redo_also).
    redo_on_source_change=False,
    run="src.producers.video_description.work:VideoDescription", pool=VISION,
)
