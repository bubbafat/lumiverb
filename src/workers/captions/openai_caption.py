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
import math
import random
import re
import sys
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

    @staticmethod
    def _repair_json(text: str) -> str:
        """Best-effort repair of JSON with unescaped quotes in string values.

        Models sometimes produce strings like:
            "a sign for "Blaster" and "Buzz Lightyear""
        which breaks JSON parsing.  We re-scan the text and escape inner
        quotes that don't serve a structural role.
        """
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch != '"':
                out.append(ch)
                i += 1
                continue

            # Opening quote of a string — find the matching close
            out.append('"')
            i += 1
            # Collect string content, escaping stray inner quotes
            while i < n:
                ch = text[i]
                if ch == '\\':
                    # Already-escaped character — pass through
                    out.append(ch)
                    i += 1
                    if i < n:
                        out.append(text[i])
                        i += 1
                    continue
                if ch == '"':
                    # Is this the real closing quote?
                    # Look ahead past whitespace for a structural char
                    j = i + 1
                    while j < n and text[j] in ' \t\n\r':
                        j += 1
                    if j >= n or text[j] in ':,}]':
                        # Structural — this is the real close
                        out.append('"')
                        i += 1
                        break
                    else:
                        # Stray inner quote — escape it
                        out.append('\\"')
                        i += 1
                        continue
                out.append(ch)
                i += 1

        return "".join(out)

    def _strip_thinking(self, text: str) -> str:
        """Strip <think>...</think> blocks; some reasoning models prefix responses with them."""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    @staticmethod
    def _sleep_with_countdown(seconds: float) -> None:
        """Sleep with a countdown timer on stderr."""
        remaining = math.ceil(seconds)
        while remaining > 0:
            sys.stderr.write(f"\r  retrying in {remaining}s ...  ")
            sys.stderr.flush()
            time.sleep(min(1.0, remaining))
            remaining -= 1
        sys.stderr.write("\r" + " " * 30 + "\r")
        sys.stderr.flush()

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
                max_edge = 1280
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
        last_raw: str = ""
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                raw = self._chat(data_url, prompt)
                last_raw = raw

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
                try:
                    parsed = json.loads(json_str)
                except json.JSONDecodeError:
                    parsed = json.loads(self._repair_json(json_str))
                description = parsed.get("description", "").strip()
                tags = [t.strip() for t in parsed.get("tags", []) if t.strip()]
                return {"description": description, "tags": tags}
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt < self.MAX_ATTEMPTS:
                    retry_after = getattr(e, "retry_after", None)
                    if retry_after:
                        sleep_time = retry_after
                    else:
                        delay = self.BACKOFF_BASE * (self.BACKOFF_MULTIPLIER ** (attempt - 1))
                        jitter = delay * self.BACKOFF_JITTER * (2 * random.random() - 1)
                        sleep_time = max(0.1, delay + jitter)
                    logger.info(
                        "Vision retry %d/%d for %s (sleeping %.1fs): %s",
                        attempt, self.MAX_ATTEMPTS, proxy_path.name, sleep_time, e,
                    )
                    self._sleep_with_countdown(sleep_time)
                    continue
                logger.warning(
                    "OpenAI-compatible caption failed for %s after %d attempts: %s\n  Last raw response: %s",
                    proxy_path, self.MAX_ATTEMPTS, e, last_raw[:500] if last_raw else "(empty)",
                )
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
                max_edge = 1280
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
            "Include text from signs, labels, products, screens, documents, or watermarks. "
            "If none, say NONE."
        )

        # Words from the prompt that should not appear in OCR results
        _prompt_noise = {
            "text", "visible", "readable", "labels", "signs", "screens",
            "documents", "watermarks", "descriptions", "none", "image",
            "products", "say", "include",
        }

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                raw = self._chat(data_url, prompt)
                text = raw.strip()
                logger.info("OCR raw response (%d chars): %s", len(text), text[:200] if text else "(empty)")
                if not text or text.upper() == "NONE" or text == "<none>":
                    return ""

                # Strip reasoning preamble (model analysis before actual OCR)
                cleaned = self._strip_ocr_reasoning(text)
                if not cleaned:
                    return ""
                logger.info("OCR found: %s", cleaned[:200])
                return cleaned
            except Exception as e:
                if attempt < self.MAX_ATTEMPTS:
                    retry_after = getattr(e, "retry_after", None)
                    if retry_after:
                        sleep_time = retry_after
                    else:
                        delay = self.BACKOFF_BASE * (self.BACKOFF_MULTIPLIER ** (attempt - 1))
                        jitter = delay * self.BACKOFF_JITTER * (2 * random.random() - 1)
                        sleep_time = max(0.1, delay + jitter)
                    logger.info(
                        "OCR retry %d/%d for %s (sleeping %.1fs): %s",
                        attempt, self.MAX_ATTEMPTS, proxy_path.name, sleep_time, e,
                    )
                    self._sleep_with_countdown(sleep_time)
                    continue
                logger.warning("OCR failed for %s after %d attempts: %s", proxy_path, self.MAX_ATTEMPTS, e)
                return ""

        return ""

    # Phrases that indicate reasoning preamble (not OCR text)
    _REASONING_STARTS = (
        "based on", "the user want", "i need to", "i will scan",
        "i'll scan", "i'll look", "let me", "looking at",
        "analyzing", "scanning", "the image show", "the image contain",
        "the image display",
    )

    def _strip_ocr_reasoning(self, text: str) -> str:
        """Clean model output: strip reasoning preamble, markdown, and noise.

        Handles both direct responses and reasoning-wrapped responses.
        """
        lines = text.splitlines()
        result_lines: list[str] = []
        in_reasoning = False

        # Check if the response starts with reasoning
        first_line_lower = lines[0].strip().lower() if lines else ""
        if any(first_line_lower.startswith(p) for p in self._REASONING_STARTS):
            in_reasoning = True

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            lower = stripped.lower()

            # Skip reasoning lines
            if in_reasoning:
                # Numbered analysis steps (1. **Scan the image:**)
                if re.match(r"^\d+\.\s+\*\*", stripped):
                    continue
                # Bullet analysis (* **Left side:** ...)
                if stripped.startswith("*") and "**" in stripped and ":" in stripped:
                    continue
                # Lines that start with reasoning phrases
                if any(lower.startswith(p) for p in self._REASONING_STARTS):
                    continue
                # "The user wants..." type lines
                if "the user" in lower and ("want" in lower or "need" in lower):
                    continue

            # Strip markdown formatting
            stripped = re.sub(r"\*\*([^*]+)\*\*", r"\1", stripped)  # **bold** → bold
            stripped = re.sub(r"^#+\s*", "", stripped)  # # Header → Header
            stripped = re.sub(r"^[-*]\s+", "", stripped)  # - bullet → text
            stripped = re.sub(r"<img>.*?</img>\s*", "", stripped)  # <img>...</img>
            stripped = stripped.strip()

            if not stripped:
                continue

            # Skip analysis/reasoning sentences
            sl = stripped.lower()
            if any(p in sl for p in (
                "scan the image", "analyze the image", "visible text",
                "i need to", "i will", "the prompt", "transcribe",
                "there's a sign", "there is a sign", "there are some",
                "looking closer", "looking at", "looking further",
                "looking again", "looking really", "let's re-eval",
                "let's look", "let me look", "let me re-",
                "wait,", "wait.", "on the left side", "on the right side",
                "on the far left", "on the far right", "in the center",
                "it's blue", "it's red", "it's white", "it's a ",
                "the background has", "the image is a", "the image show",
                "it looks like", "but they are", "but it's",
                "re-examine", "final check", "so the text is",
                "so the full text", "text found:", "partially visible",
                "too small", "too far", "too blurry", "not legible",
                "no clear text", "no obvious", "no text",
                "in red letters", "in large", "in white",
                "with white letters", "with blue letters",
            )):
                continue

            result_lines.append(stripped)

        result_lines = self._dedup_lines(result_lines)
        result = "\n".join(result_lines).strip()

        # Final check: if result is just "NONE" variants
        if result.upper() in ("NONE", "NONE.", "<NONE>", "N/A"):
            return ""

        return result

    @staticmethod
    def _dedup_lines(lines: list[str]) -> list[str]:
        """Collapse consecutive repeating patterns (1+ lines) to at most 2."""
        if not lines:
            return lines
        out: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            matched = False
            # Try pattern lengths k = 1, 2, ... up to what could repeat 3+ times
            for k in range(1, (n - i) // 3 + 1):
                pattern = lines[i : i + k]
                count = 1
                j = i + k
                while j + k <= n and lines[j : j + k] == pattern:
                    count += 1
                    j += k
                if count > 2:
                    out.extend(pattern)
                    out.extend(pattern)
                    i = j  # skip past all repetitions
                    matched = True
                    break
            if not matched:
                out.append(lines[i])
                i += 1
        return out

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
            if resp.status_code == 429:
                from src.workers.captions.retry_after import parse_retry_after
                retry_after = parse_retry_after(resp)
                err = requests.HTTPError(response=resp)
                err.retry_after = retry_after  # type: ignore[attr-defined]
                raise err
        resp.raise_for_status()

        msg = resp.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        # Reasoning models (e.g. Qwen 3.5) may put the answer in reasoning_content
        # when content is empty — fall back to it
        if not content.strip():
            content = msg.get("reasoning_content") or ""
        return self._strip_thinking(content)
