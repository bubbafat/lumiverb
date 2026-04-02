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
import random
import re
import time
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

    The model ID is passed at construction time (auto-discovered or from config).
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

    # Retry config: 3 attempts with exponential backoff + jitter.
    # Base delays: ~1s, ~3s, ~9s (jittered ±50%).
    MAX_ATTEMPTS = 3
    BACKOFF_BASE = 1.0
    BACKOFF_MULTIPLIER = 3.0
    BACKOFF_JITTER = 0.5  # ±50%

    def describe(self, proxy_path: Path) -> dict:
        """
        Returns {} on failure.

        Retries with exponential backoff + jitter to reduce pressure on the
        inference server under load.
        """
        if not proxy_path.exists():
            logger.warning("Proxy not found: %s", proxy_path)
            return {}

        # Precompute request inputs once; on retry we only re-call the API.
        try:
            img = Image.open(proxy_path)
            try:
                max_edge = 1024
                if max(img.width, img.height) > max_edge:
                    img.thumbnail((max_edge, max_edge), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                image_b64 = base64.b64encode(buf.getvalue()).decode()
                data_url = f"data:image/jpeg;base64,{image_b64}"
            finally:
                img.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("OpenAI-compatible caption failed for %s: %s", proxy_path, e)
            return {}

        prompt = (
            "Describe this image in 2-3 sentences, being specific about "
            "the subject, setting, and mood. Then provide 5-10 descriptive "
            "tags. Respond only with valid JSON in this exact format:\n"
            '{"description": "...", "tags": ["tag1", "tag2", ...]}'
        )

        last_error: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                raw = self._chat(data_url, prompt)

                # Strip markdown code fences if present
                clean = raw.strip()
                if not clean:
                    raise ValueError("Empty completion content")
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
                last_error = e
                if attempt < self.MAX_ATTEMPTS:
                    delay = self.BACKOFF_BASE * (self.BACKOFF_MULTIPLIER ** (attempt - 1))
                    jitter = delay * self.BACKOFF_JITTER * (2 * random.random() - 1)
                    sleep_time = max(0.1, delay + jitter)
                    logger.info(
                        "Vision retry %d/%d for %s (sleeping %.1fs): %s",
                        attempt, self.MAX_ATTEMPTS, proxy_path.name, sleep_time, e,
                    )
                    time.sleep(sleep_time)
                    continue
                logger.warning("OpenAI-compatible caption failed for %s after %d attempts: %s", proxy_path, self.MAX_ATTEMPTS, e)
                return {}

        return {}

    def extract_text(self, proxy_path: Path) -> str:
        """Extract visible text from an image via OCR prompt.

        Returns the extracted text as a string, or empty string if none found.
        Uses the same retry logic as describe().
        """
        if not proxy_path.exists():
            return ""

        try:
            img = Image.open(proxy_path)
            try:
                max_edge = 1024
                if max(img.width, img.height) > max_edge:
                    img.thumbnail((max_edge, max_edge), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                image_b64 = base64.b64encode(buf.getvalue()).decode()
                data_url = f"data:image/jpeg;base64,{image_b64}"
            finally:
                img.close()
        except Exception as e:
            logger.warning("OCR image prep failed for %s: %s", proxy_path, e)
            return ""

        prompt = (
            "What text is visible in this image? "
            "List all readable text you can see — labels, signs, screens, documents, watermarks, anything. "
            "Just the text, no descriptions. "
            "If none, say NONE."
        )

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                raw = self._chat(data_url, prompt)
                text = raw.strip()
                logger.info("OCR raw response: %s", text[:200] if text else "(empty)")
                if not text or text.upper() == "NONE":
                    return ""
                return text
            except Exception as e:
                if attempt < self.MAX_ATTEMPTS:
                    delay = self.BACKOFF_BASE * (self.BACKOFF_MULTIPLIER ** (attempt - 1))
                    jitter = delay * self.BACKOFF_JITTER * (2 * random.random() - 1)
                    sleep_time = max(0.1, delay + jitter)
                    logger.info(
                        "OCR retry %d/%d for %s (sleeping %.1fs): %s",
                        attempt, self.MAX_ATTEMPTS, proxy_path.name, sleep_time, e,
                    )
                    time.sleep(sleep_time)
                    continue
                logger.warning("OCR failed for %s after %d attempts: %s", proxy_path, self.MAX_ATTEMPTS, e)
                return ""

        return ""

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
