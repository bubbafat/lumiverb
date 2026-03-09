"""
Qwen VL caption provider via LM Studio OpenAI-compatible API.

LM Studio exposes POST /v1/chat/completions with vision support.
Images are sent as base64 data URLs in the message content.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

import requests

from src.workers.captions.base import CaptionProvider

logger = logging.getLogger(__name__)


class QwenCaptionProvider(CaptionProvider):
    """
    Calls a locally-running LM Studio instance serving a Qwen VL model.

    Config (from Settings):
        lmstudio_url:           http://localhost:1234/v1
        lmstudio_vision_model:  qwen2.5-vl-7b-instruct (or whatever is loaded)
    """

    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

    @property
    def provider_id(self) -> str:
        return "qwen_lmstudio"

    def _strip_thinking(self, text: str) -> str:
        """Strip <think>...</think> blocks; some reasoning models prefix responses with them."""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    def describe(self, proxy_path: Path) -> dict:
        if not proxy_path.exists():
            logger.warning("Proxy not found: %s", proxy_path)
            return {}
        try:
            image_b64 = base64.b64encode(proxy_path.read_bytes()).decode()
            data_url = f"data:image/jpeg;base64,{image_b64}"

            # Two-shot: first get description, then tags
            description = self._chat(
                data_url,
                "Describe this image in one or two sentences. "
                "Be specific about the subject, setting, and mood.",
            )
            tags_raw = self._chat(
                data_url,
                "List 5-10 descriptive tags for this image as a "
                "comma-separated list. Output only the tags, nothing else.",
            )
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
            return {"description": description, "tags": tags}

        except Exception as e:  # noqa: BLE001
            logger.warning("Qwen caption failed for %s: %s", proxy_path, e)
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
            "max_tokens": 300,
            "temperature": 0.2,
        }
        resp = requests.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return self._strip_thinking(content)

