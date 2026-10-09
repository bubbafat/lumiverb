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
VISION_DEFAULTS = {"model": "", "prompt": VISION_PROMPT, "max_edge": 1280, "temperature": 0.2, "max_tokens": 500}
