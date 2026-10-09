"""The prompts the vision producers send: output-affecting settings, so
changing one makes their descriptions stale."""

VISION_PROMPT = (
    "Describe this image in 2-3 sentences, being specific about "
    "the subject, setting, and mood. Then provide 5-10 descriptive "
    "tags. Respond only with valid JSON in this exact format:\n"
    '{"description": "...", "tags": ["tag1", "tag2", ...]}'
)
OCR_PROMPT = (
    "What text is visible in this image? "
    "Include text from signs, labels, products, screens, documents, or watermarks. "
    "If none, say NONE."
)


def ai_settings(prompt: str, fixed: str = "") -> tuple:
    """A vision machine's settings: its model (Settings → AI), the prompt,
    the image's size and the sampling."""
    from src.producers.contract import ITS_JOBS, Setting

    return (
        Setting("model", "", "Model", kind="text", fixed=ITS_JOBS),
        Setting("prompt", prompt, "Prompt", kind="text", fixed=fixed),
        Setting("max_edge", 1280, "Image size sent", minimum=256, maximum=4096, unit="px", fixed=fixed),
        Setting("temperature", 0.2, "Temperature", kind="float", minimum=0, maximum=2, advanced=True, fixed=fixed),
        Setting("max_tokens", 500, "Longest answer", minimum=50, maximum=4000, unit="tokens", advanced=True,
                fixed=fixed),
    )
