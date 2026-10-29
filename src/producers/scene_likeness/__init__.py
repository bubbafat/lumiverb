"""Visual search in videos: a CLIP vector of each scene's picture, by the CLIP
job's machines (ADR-022), with the model photos' vectors are made with
(src/producers/clip/), so a scene is compared as a photo is.

Its scenes are the ones its video's scene descriptions are made for: a
redo's found scenes while it has them (shown once described), else the ones
shown. Found again, a scene whose picture is the frame of one shown keeps
that one's vector (VideoSceneRepository.keep_descriptions): the same frame
makes the same vector, so only new frames are embedded. A video with a new
frame that has none is missing again (made, below) whatever its lineage
says, so nothing goes with its scenes when they're found again (it's in no
redo_also: starting over would take the lineage of a video whose vectors
were all kept, and its redo would make every one again)."""

from src.producers import names
from src.producers.contract import VIDEO, Data, Field, ProducerSpec, Setting
from src.producers.pools import CLIP

# Its model is the CLIP job's (Settings › AI), photos' too: one model across the library.
ONE_MODEL = "Another model makes every photo's and scene's vectors again; not offered yet."

MAKES = Data(
    names.SCENE_LIKENESS, "What visual search compares in a video: each scene's picture's CLIP vector, and "
    "the model it's from.",
    (Field("start_ms", "number"), Field("end_ms", "number"), Field("vector", "vector"), Field("model", "text")),
    stored=("video_scenes",), many=True,
)

# Of the video a, the scenes it embeds: a redo's found ones while it has them, else the ones shown (never a
# redo's still being found). What GET /v1/video/{id}/scenes?describing=true lists.
ITS_SCENES = ("(s.staged = 'found' OR (s.staged IS NULL AND NOT EXISTS (SELECT 1 FROM video_scenes f"
              " WHERE f.asset_id = a.asset_id AND f.staged = 'found')))")

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.ANALYSIS_PROXY, names.SCENES),
    artifact=names.SCENE_LIKENESS, version="1", media=VIDEO, unit="second",
    title="Visual search in videos (CLIP)", order=91,
    # A video whose scenes are found (as scene descriptions); not a redo's still being found.
    applies=("a.media_type = 'video' AND a.video_indexed"
             " AND EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id"
             " AND s.staged IS DISTINCT FROM 'finding')"),
    # Made in parts: once each of its scenes has a vector (the route records its lineage then).
    made=("NOT EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id AND s.likeness IS NULL"
          f" AND {ITS_SCENES})"),
    settings=(
        Setting("model", "ViT-B-32", "Model", kind="text", fixed=ONE_MODEL),
        Setting("pretrained", "openai", "Weights", kind="text", fixed=ONE_MODEL),
    ),
    uniform=True,
    # Its frames are the analysis proxy's: a file changed in place is a new clip (#37).
    redo_on_source_change=False,
    # The CLIP job's machines (Settings › AI), as photos' vectors.
    run="src.producers.scene_likeness.work:SceneLikeness", pool=CLIP,
)
