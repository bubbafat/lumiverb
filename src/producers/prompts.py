"""The prompts the vision producers send: output-affecting settings, so
changing one makes their descriptions stale."""

VISION_PROMPT = (
    "Describe this image in 2-3 sentences, being specific about "
    "the subject, setting, and mood. Then provide 5-10 descriptive "
    "tags. Respond only with valid JSON in this exact format:\n"
    '{"description": "...", "tags": ["tag1", "tag2", ...]}'
)
# A scene's frame, one still of a video (Robert, Oct 29: the study of 70 videos, S1). The photos' prompt asked for
# mood, which filled a third of every scene with it, and each scene guessed a landmark on its own ("likely Disney's
# Hollywood Studios" on Main Street): a name only when it's written in the frame, and a dark frame said in a few words.
SCENE_PROMPT = (
    "This is one still frame from a video. Say in one or two plain sentences what it shows: who is there (by how "
    "they look, never by name), what they are doing, and where, as far as the frame shows it. Copy any words you "
    "can read on signs, menus or screens exactly, in quotes. Name a place, brand or ride only when its name is "
    "written in the frame. No mood, lighting, atmosphere or praise words, and don't begin with 'The image' or "
    "'This image'. If the frame is dark, blurred or shows nothing clear, say so in a few words. Then provide 5-10 "
    "descriptive tags. Respond only with valid JSON in this exact format:\n"
    '{"description": "...", "tags": ["tag1", "tag2", ...]}'
)
# A video's own description, written from its scenes' (they come before it, in order: video_description.work.scenes_text).
# What it is first, then what to find it by (Robert, Oct 29, P5 of the study): asked one scene at a time, a model
# listed the scenes, a sentence each, and never said what the video was. The reply's gist and details are its
# description (openai_caption's _parse_description). A name only when quoted, and nothing about what isn't there
# (Robert, Oct 29, Qwen and Haiku compared): Haiku 5.5 opened with "most likely Castaway Cay" and spent a detail on
# "No readable signs appear"; where a clip was is its location's to say.
VIDEO_PROMPT = (
    "These are moments from one video, in order, each described from a single still frame by someone who saw only "
    "that frame. Work out what the video is of as a whole.\n"
    "The frames are one continuous video: a person or place described a little differently in two frames is the "
    "same one. Where frames disagree, keep what most say and drop the rest. Ignore dark, blurred or empty frames.\n"
    "Write plainly, like a careful viewer's notes: no mood, lighting, praise or sales words (serene, vibrant, "
    "luxurious, whimsical, bustling), no 'the video shows', and never 'scene', 'frame', 'then', 'next' or 'later'.\n"
    "Name a place, park, ride, restaurant or brand only when its name is quoted in the moments; never work one out "
    "from what things look like. Say only what is there, never that something can't be read or isn't seen.\n"
    "Respond only with valid JSON in this exact format:\n"
    '{"gist": "one sentence: what the video is of (an outing, a meal, a walk through somewhere, a ride, a tour, a '
    'show) and where, as the frames show it", "details": ["3 to 5 short sentences, fewer for a short, simple video, '
    'each a different thing worth finding it by: what people do, notable things, words read on signs, menus or '
    'screens copied exactly in quotes"], "tags": ["5-10 tags"]}'
)
OCR_PROMPT = (
    "What text is visible in this image? "
    "Include text from signs, labels, products, screens, documents, or watermarks. "
    "If none, say NONE."
)


def text_ai_settings(prompt: str, *, max_tokens: int) -> tuple:
    """A vision machine's settings for text alone (no image): its model, the prompt and the sampling."""
    from src.producers.contract import ITS_JOBS, Setting

    return (
        Setting("model", "", "Model", kind="text", fixed=ITS_JOBS),
        Setting("prompt", prompt, "Prompt", kind="text"),
        Setting("temperature", 0.2, "Temperature", kind="float", minimum=0, maximum=2, advanced=True),
        Setting("max_tokens", max_tokens, "Longest answer", minimum=50, maximum=4000, unit="tokens",
                advanced=True),
    )


def ai_settings(prompt: str, fixed: str = "", *, room_fixed: bool = True) -> tuple:
    """A vision machine's settings: its model (Settings → AI), the prompt,
    the image's size and the sampling. room_fixed=False offers the longest
    answer even where the rest are fixed: a model that thinks first spends
    it thinking, and needs more than the default to answer at all."""
    from src.producers.contract import ITS_JOBS, Setting

    return (
        Setting("model", "", "Model", kind="text", fixed=ITS_JOBS),
        Setting("prompt", prompt, "Prompt", kind="text", fixed=fixed),
        # No larger than the proxies the images come from (PROXY_CACHE_EDGE):
        # a larger size would send the same image and redo every clip for nothing.
        Setting("max_edge", 1280, "Image size sent", minimum=256, maximum=1280, unit="px", fixed=fixed),
        Setting("temperature", 0.2, "Temperature", kind="float", minimum=0, maximum=2, advanced=True, fixed=fixed),
        Setting("max_tokens", 500, "Longest answer", minimum=50, maximum=4000, unit="tokens", advanced=True,
                fixed=fixed if room_fixed else ""),
    )
