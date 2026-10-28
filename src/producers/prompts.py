"""The prompts the vision producers send: output-affecting settings, so
changing one makes their descriptions stale."""

VISION_PROMPT = (
    "Describe this image in 2-3 sentences, being specific about "
    "the subject, setting, and mood. Then provide 5-10 descriptive "
    "tags. Respond only with valid JSON in this exact format:\n"
    '{"description": "...", "tags": ["tag1", "tag2", ...]}'
)
# A video's own description, written from its scenes' (they come before it, in order: video_description.work.scenes_text).
VIDEO_PROMPT = (
    "These are the scenes of one video, in order, each described from a single frame, with when it starts. "
    "Say plainly what the video shows, as a careful viewer would note it: where it is, who or what is in it, "
    "and what happens, in order. Use only what the scenes say. No praise, feelings or sales words "
    "(luxurious, idyllic, breathtaking, evokes), and no guessing at what isn't shown. Short sentences: 2 for a "
    "short, simple video, up to 8 for a long one with more going on, never padding. Don't mention scenes, "
    "frames or times. Then provide 5-10 descriptive tags. Respond only with valid JSON in this exact "
    "format:\n"
    '{"description": "...", "tags": ["tag1", "tag2", ...]}'
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


def ai_settings(prompt: str, fixed: str = "") -> tuple:
    """A vision machine's settings: its model (Settings → AI), the prompt,
    the image's size and the sampling."""
    from src.producers.contract import ITS_JOBS, Setting

    return (
        Setting("model", "", "Model", kind="text", fixed=ITS_JOBS),
        Setting("prompt", prompt, "Prompt", kind="text", fixed=fixed),
        # No larger than the proxies the images come from (PROXY_CACHE_EDGE):
        # a larger size would send the same image and redo every clip for nothing.
        Setting("max_edge", 1280, "Image size sent", minimum=256, maximum=1280, unit="px", fixed=fixed),
        Setting("temperature", 0.2, "Temperature", kind="float", minimum=0, maximum=2, advanced=True, fixed=fixed),
        Setting("max_tokens", 500, "Longest answer", minimum=50, maximum=4000, unit="tokens", advanced=True,
                fixed=fixed),
    )
