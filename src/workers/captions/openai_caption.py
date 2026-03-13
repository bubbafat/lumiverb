"""
OpenAI-compatible vision caption provider.

Works with any OpenAI-compatible API (LM Studio, Ollama, vLLM, etc.).
Images are sent as base64 data URLs in the message content.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from pathlib import Path

import requests
from PIL import Image

from src.workers.captions.base import CaptionProvider

logger = logging.getLogger(__name__)


class OpenAICompatibleCaptionProvider(CaptionProvider):
    """
    Calls any OpenAI-compatible vision API.

    Config (from Settings):
        vision_api_url: base URL (e.g. http://localhost:1234/v1)

    The model ID is passed at construction time (from library.vision_model_id).
    """

    def __init__(self, base_url: str, model: str, api_key: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key

    @property
    def provider_id(self) -> str:
        return "openai_compatible"

    def _extract_first_json_object(self, text: str) -> str | None:
        """Extract the first complete {...} JSON object from text using brace counting."""
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(text[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return None

    def _strip_thinking(self, text: str) -> str:
        """Strip <think>...</think> blocks; some reasoning models prefix responses with them."""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    def describe(self, proxy_path: Path) -> dict:
        if not proxy_path.exists():
            logger.warning("Proxy not found: %s", proxy_path)
            return {}
        try:
            img = Image.open(proxy_path)
            max_edge = 1024
            if max(img.width, img.height) > max_edge:
                img.thumbnail((max_edge, max_edge), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            image_b64 = base64.b64encode(buf.getvalue()).decode()
            data_url = f"data:image/jpeg;base64,{image_b64}"

            prompt = (
                "Describe this image in 2-3 sentences, being specific about "
                "the subject, setting, and mood. Then provide 5-10 descriptive "
                "tags. Respond only with valid JSON in this exact format:\n"
                '{"description": "...", "tags": ["tag1", "tag2", ...]}'
            )
            raw = self._chat(data_url, prompt)

            # Strip markdown code fences if present
            clean = raw.strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```[a-z]*\n?", "", clean)
                clean = re.sub(r"\n?```$", "", clean)
                clean = clean.strip()

            json_str = self._extract_first_json_object(clean)
            if not json_str:
                raise ValueError(f"No JSON object found in response: {clean[:100]!r}")
            parsed = json.loads(json_str)
            description = parsed.get("description", "").strip()
            tags = [t.strip() for t in parsed.get("tags", []) if t.strip()]
            return {"description": description, "tags": tags}

        except Exception as e:  # noqa: BLE001
            logger.warning("OpenAI-compatible caption failed for %s: %s", proxy_path, e)
            return {}

    def _chat(self, data_url: str, prompt: str) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "max_tokens": 500,
            "temperature": 0.2,
        }
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        resp = requests.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=60,
        )
        if not resp.ok:
            logger.warning("Error from AI Provider: %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()

        content = resp.json()["choices"][0]["message"]["content"]
        return self._strip_thinking(content)
