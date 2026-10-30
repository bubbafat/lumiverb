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

from src.processing.workers.captions.base import (
    LONGEST_ANSWER,
    CaptionError,
    CaptionProvider,
    OutOfRoomError,
    is_endpoint_fault,
    out_of_room,
)
from src.shared import ai_requests

logger = logging.getLogger(__name__)


class OpenAICompatibleCaptionProvider(CaptionProvider):
    """
    Calls any OpenAI-compatible vision API.

    Config (from Settings):
        vision_api_url: base URL (e.g. http://localhost:1234/v1)

    The model ID is passed at construction time (auto-discovered or from config).
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        *,
        settings: dict | None = None,
        ocr_settings: dict | None = None,
        fingerprint: str = "",
    ) -> None:
        from src.producers.names import DESCRIPTION, TEXT
        from src.shared.producers import effective_settings

        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        # The certificate an https machine was added with (src/shared/ai_requests.py).
        self._fingerprint = fingerprint
        # Output-affecting settings (prompt, image size, sampling): the server's,
        # so what's recorded in lineage is what was sent. Defaults otherwise.
        self._vision = settings or effective_settings(DESCRIPTION)
        self._ocr = ocr_settings or effective_settings(TEXT)
        # A machine that refuses any temperature (Claude 4.7 and later, OpenAI's reasoning models) is asked
        # without one from then on: it answers with its own.
        self._sends_temperature = True

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

    @staticmethod
    def _gist_and_details(parsed: dict) -> str:
        """A reply of a gist and its details (a video's prompt, VIDEO_PROMPT) as one description: the gist, then each
        detail, a sentence each; "" for neither."""
        details = parsed.get("details")
        said = [parsed.get("gist"), *(details if isinstance(details, list) else [])]
        sentences = [s.strip() for s in said if isinstance(s, str) and s.strip()]
        return " ".join(s if s[-1] in ".!?" else s + "." for s in sentences)

    def _parse_description(self, raw: str) -> dict:
        """{"description", "tags"} from the model's reply; ValueError when it holds none."""
        # Strip markdown code fences if present
        clean = raw.strip()
        if not clean:
            raise ValueError("Empty completion content")
        if clean.startswith("```"):
            clean = re.sub(r"^```[a-z]*\n?", "", clean)
            clean = re.sub(r"\n?```$", "", clean)
            clean = clean.strip()

        # The first balanced object; else first { to last } (a stray quote
        # throws the brace count off), each as is or with its quotes repaired.
        candidates = [self._extract_first_json_object(clean)]
        start, end = clean.find("{"), clean.rfind("}")
        if start != -1 and end > start:
            candidates.append(clean[start:end + 1])
        for json_str in filter(None, candidates):
            for text in (json_str, self._repair_json(json_str)):
                try:
                    parsed = json.loads(text, strict=False)  # a newline inside a string
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    description = str(parsed.get("description") or "").strip() or self._gist_and_details(parsed)
                    tags = [t.strip() for t in parsed.get("tags") or [] if isinstance(t, str) and t.strip()]
                    return {"description": description, "tags": tags}
        salvaged = self._salvage(clean)
        if salvaged is not None:
            logger.warning("AI reply cut off (%d chars); kept its description and %d whole tags",
                           len(clean), len(salvaged["tags"]))
            return salvaged
        # Not the reply itself: a clip's failure is seen by everyone who sees the clip; the log keeps the reply.
        raise ValueError(f"No JSON object found in response ({len(clean)} chars)")

    _WHOLE_STRING = re.compile(r'\s*("(?:[^"\\]|\\.)*")\s*(?=[,\]}])[,]?', re.DOTALL)
    MAX_SALVAGED_TAGS = 10

    def _salvage(self, text: str) -> dict | None:
        """A reply cut off after its description (max_tokens, a loop in the
        tags): the description, and the tags that are whole (no repeats, at
        most MAX_SALVAGED_TAGS). A video's is its gist and the details that
        are whole (VIDEO_PROMPT). None when neither is whole."""
        description = " ".join(self._whole_strings(text, "description")[:1])
        if not description:
            gist = self._whole_strings(text, "gist")[:1]
            description = self._gist_and_details({"gist": gist[0], "details": self._whole_strings(
                text, "details", listed=True)}) if gist else ""
        if not description:
            return None
        tags = list(dict.fromkeys(self._whole_strings(text, "tags", listed=True)))
        return {"description": description, "tags": tags[:self.MAX_SALVAGED_TAGS]}

    def _whole_strings(self, text: str, field: str, *, listed: bool = False) -> list[str]:
        """The field's text when it's whole, or (listed) its list's strings up to the first that isn't: [] for
        none."""
        start = re.search(rf'"{field}"\s*:\s*' + (r"\[" if listed else ""), text)
        found: list[str] = []
        at = start.end() if start else len(text)
        while start is not None and (m := self._WHOLE_STRING.match(text, at)) is not None:
            try:
                value = json.loads(m.group(1), strict=False).strip()
            except json.JSONDecodeError:
                break
            if value:
                found.append(value)
            if not listed:
                break
            at = m.end()
        return found

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
        {"description", "tags"}. Raises CaptionError when it can't, saying
        whether the endpoint was at fault (then vision work stops, and the
        clip isn't charged).

        Retries with exponential backoff + jitter to reduce pressure on the
        inference server under load.
        """
        if not proxy_path.exists():
            raise CaptionError(f"Proxy not found: {proxy_path.name}", endpoint_fault=False)

        # Precompute request inputs once; on retry we only re-call the API.
        try:
            img = Image.open(proxy_path)
            try:
                max_edge = int(self._vision["max_edge"])
                if max(img.width, img.height) > max_edge:
                    img.thumbnail((max_edge, max_edge), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                image_b64 = base64.b64encode(buf.getvalue()).decode()
                data_url = f"data:image/jpeg;base64,{image_b64}"
            finally:
                img.close()
        except Exception as e:  # noqa: BLE001
            raise CaptionError(f"Couldn't read the image: {e}", endpoint_fault=False) from e

        return self._described(proxy_path.name, [{"type": "image_url", "image_url": {"url": data_url}},
                                                 {"type": "text", "text": self._vision["prompt"]}])

    def describe_text(self, text: str) -> dict:
        """{"description", "tags"} from text alone (a video's scenes, in order): the text, then the prompt.
        Raises CaptionError as describe() does."""
        return self._described("text", self._text_then_prompt(text, self._vision))

    @staticmethod
    def _text_then_prompt(text: str, settings: dict) -> list[dict]:
        """The prompt last: a machine cuts a request longer than its context from the start (Ollama), and the
        model, given the text without it, answers something else."""
        return [{"type": "text", "text": f"{text}\n\n{settings['prompt']}"}]

    def _described(self, what: str, content: list[dict]) -> dict:
        last_error: Exception | None = None
        last_raw: str = ""
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                got = self._asked(content, self._vision)
                last_raw = got["reply"]
                return self._with_room(content, got)
            except Exception as e:  # noqa: BLE001
                last_error = e
                # Out of room at the most it was given: the same again would end the same.
                if attempt < self.MAX_ATTEMPTS and not isinstance(e, OutOfRoomError):
                    retry_after = getattr(e, "retry_after", None)
                    if retry_after:
                        sleep_time = retry_after
                    else:
                        delay = self.BACKOFF_BASE * (self.BACKOFF_MULTIPLIER ** (attempt - 1))
                        jitter = delay * self.BACKOFF_JITTER * (2 * random.random() - 1)
                        sleep_time = max(0.1, delay + jitter)
                    logger.info(
                        "Vision retry %d/%d for %s (sleeping %.1fs): %s",
                        attempt, self.MAX_ATTEMPTS, what, sleep_time, e,
                    )
                    self._sleep_with_countdown(sleep_time)
                    continue
                logger.warning(
                    "OpenAI-compatible caption failed for %s after %d attempts: %s\n  Last raw response: %s",
                    what, attempt, e, last_raw[:500] if last_raw else "(empty)",
                )
                raise CaptionError(str(e) or type(e).__name__, endpoint_fault=is_endpoint_fault(e)) from e

        raise CaptionError(str(last_error), endpoint_fault=False)

    def _given_room(self, content: list[dict], settings: dict, got: dict) -> tuple[list[dict], int]:
        """A reply (_asked's) cut off at its longest answer is asked again once, with twice the room (at most
        LONGEST_ANSWER), and the log says to raise it when that one finishes. The replies, the last asked first,
        and the room the last had."""
        limit = int(settings["max_tokens"])
        if got["finish"] != "length" or limit >= LONGEST_ANSWER:
            return [got], limit
        roomier = min(2 * limit, LONGEST_ANSWER)
        again = self._asked(content, {**settings, "max_tokens": roomier})
        if again["finish"] != "length":
            logger.warning("An AI reply cut off at its Longest answer (%d tokens) finished when asked again with %d: "
                           "raise its Longest answer in Settings › Processing", limit, roomier)
        return [again, got], roomier

    def _with_room(self, content: list[dict], got: dict) -> dict:
        """The description in a reply (_asked's), given more room when it's cut off (_given_room); still cut off,
        what's whole in the longer reply is kept (_salvage), else OutOfRoomError."""
        replies, limit = self._given_room(content, self._vision, got)
        if replies[0]["finish"] != "length":
            return self._parse_description(replies[0]["reply"])
        for reply in replies:
            try:
                return self._parse_description(reply["reply"])
            except ValueError:
                if reply["finish"] != "length":
                    raise
        raise OutOfRoomError(limit, said_nothing=not any(r["reply"].strip() for r in replies))

    def extract_text(self, proxy_path: Path) -> str:
        """Extract visible text from an image via OCR prompt.

        Returns the extracted text as a string, or empty string if none found.
        Raises CaptionError when it can't tell (as describe() does): "" is
        only ever "no text".
        Uses the same retry logic as describe().
        """
        if not proxy_path.exists():
            raise CaptionError(f"Proxy not found: {proxy_path.name}", endpoint_fault=False)

        try:
            img = Image.open(proxy_path)
            try:
                max_edge = int(self._ocr["max_edge"])
                if max(img.width, img.height) > max_edge:
                    img.thumbnail((max_edge, max_edge), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                image_b64 = base64.b64encode(buf.getvalue()).decode()
                data_url = f"data:image/jpeg;base64,{image_b64}"
            finally:
                img.close()
        except Exception as e:
            raise CaptionError(f"Couldn't read the image: {e}", endpoint_fault=False) from e

        prompt = self._ocr["prompt"]
        content = [{"type": "image_url", "image_url": {"url": data_url}}, {"type": "text", "text": prompt}]
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                replies, limit = self._given_room(content, self._ocr, self._asked(content, self._ocr))
                if replies[0]["finish"] == "length":
                    # Text cut off isn't the text it reads, nor is nothing "no text".
                    raise OutOfRoomError(limit, said_nothing=not any(r["reply"].strip() for r in replies))
                text = replies[0]["reply"].strip()
                logger.info("OCR raw response (%d chars): %s", len(text), text[:200] if text else "(empty)")
                cleaned = self._text_read(text)
                if cleaned:
                    logger.info("OCR found: %s", cleaned[:200])
                return cleaned
            except Exception as e:
                # Out of room at the most it was given: the same again would end the same.
                if attempt < self.MAX_ATTEMPTS and not isinstance(e, OutOfRoomError):
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
                logger.warning("OCR failed for %s after %d attempts: %s", proxy_path, attempt, e)
                raise CaptionError(str(e) or type(e).__name__, endpoint_fault=is_endpoint_fault(e)) from e

        return ""

    # The prompt's "If none, say NONE.", alone or with why (Claude Haiku 5.5: "NONE. I don't see any legible text in
    # this image. It shows a beach sunset…"): never a sign reading "NONE SHALL PASS".
    _NONE = re.compile(r"\s*<?none>?[ \t]*(?:[.,:;!\-\u2013\u2014(\n]|$)", re.IGNORECASE)

    @classmethod
    def _says_none(cls, reply: str) -> bool:
        """The reply says the image has no text."""
        return not reply.strip() or cls._NONE.match(reply) is not None

    @classmethod
    def _text_listed(cls, reply: str) -> list[str] | None:
        """The texts a reply lists as OCR_PROMPT asks ({"text": [...]}), each once, in order; None when it isn't
        that (a prompt of an admin's own, or a model that didn't)."""
        body = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            got = json.loads(body[start:end + 1])
        except ValueError:
            return None
        listed = got.get("text") if isinstance(got, dict) else None
        if not isinstance(listed, list) or not all(isinstance(t, str) for t in listed):
            return None
        return list(dict.fromkeys(t.strip() for t in listed if t.strip()))

    def _text_read(self, reply: str) -> str:
        """The text a reply read, a line each, or "" for none: its list (_text_listed), else NONE (_says_none), else
        its lines without what reads as a model's reasoning (_strip_ocr_reasoning)."""
        listed = self._text_listed(reply)
        if listed is not None:
            return "\n".join(listed)
        if self._says_none(reply):
            return ""
        return self._strip_ocr_reasoning(reply)

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

    def _chat(self, data_url: str, prompt: str, settings: dict) -> str:
        return self._ask([{"type": "image_url", "image_url": {"url": data_url}}, {"type": "text", "text": prompt}],
                         settings)

    def _ask(self, content: list[dict], settings: dict) -> str:
        """One request: the content (an image and the prompt, or text alone), the reply's text."""
        return self._asked(content, settings)["reply"]

    def _asked(self, content: list[dict], settings: dict, *, timeout: float = 60) -> dict:
        """One request, and what came back: {"reply": its text, "finish": why it ended ("stop", "length"…, or
        None), "tokens": {"prompt", "answer"} when the machine says what it used, else None}."""
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": int(settings["max_tokens"]),
            "temperature": float(settings["temperature"]),
        }
        if not self._sends_temperature:
            del payload["temperature"]
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._base_url}/chat/completions"
        # Its address checked, no redirect followed (Redirected: the machine's fault, as is_endpoint_fault says).
        resp = ai_requests.post(url, json=payload, headers=headers, timeout=timeout, fingerprint=self._fingerprint)
        if resp.status_code == 400 and "temperature" in payload and "temperature" in resp.text.lower():
            logger.warning("%s won't take a temperature (%s): asking it without one from now on",
                           self._base_url, resp.text[:200])
            self._sends_temperature = False
            return self._asked(content, settings, timeout=timeout)
        if not resp.ok:
            # In Lumiverb's words: what the machine said is the log's (it reaches every signed-in person otherwise,
            # as the machine's status, why work waits, or a clip's failure).
            err = requests.HTTPError(f"{url} {ai_requests.answered(resp, model=self._model, api='chat completions')}.",
                                     response=resp)
            if resp.status_code == 429:
                from src.processing.workers.captions.retry_after import parse_retry_after
                err.retry_after = parse_retry_after(resp)  # type: ignore[attr-defined]
            raise err

        choice = resp.json()["choices"][0]
        msg = choice["message"]
        content = msg.get("content") or ""
        finish = choice.get("finish_reason")
        logger.debug("AI reply (finish_reason=%s): %s", finish, content[:2000])
        if finish not in (None, "stop"):
            logger.warning("AI reply ended with finish_reason=%s after %d chars (max_tokens %s)",
                           finish, len(content), payload["max_tokens"])
        # Reasoning models (e.g. Qwen 3.5) may put the answer in reasoning_content
        # when content is empty — fall back to it
        if not content.strip():
            content = msg.get("reasoning_content") or ""
        usage = resp.json().get("usage") or {}
        used = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return {"reply": self._strip_thinking(content), "finish": finish,
                "tokens": {"prompt": used[0], "answer": used[1]} if all(isinstance(n, int) for n in used) else None}

    def answer(self, prompt: str, *, max_tokens: int, timeout: float) -> str:
        """A question in words alone, and the reply's text (an AI-assisted search's: src/server/search/assist.py).
        Raises what the request raises."""
        return self._asked([{"type": "text", "text": prompt}], {"max_tokens": max_tokens, "temperature": 0.0},
                           timeout=timeout)["reply"]

    def tried(self, what: str, *, image: bytes | None = None, text: str = "", timeout: float = 45) -> dict:
        """One request as a job's work makes it, tried once (Settings › AI's Test): what is "describe" (an image's
        description and tags, this provider's settings), "read" (its text, the OCR settings) or "describe_text"
        (from text alone, a video's scenes). _asked's answer, with "result": what the work would save (the
        description and tags; the text read), or None with "problem" saying why it couldn't be. Raises what the
        request raises (requests' errors)."""
        settings = self._ocr if what == "read" else self._vision
        if what == "describe_text":
            content = self._text_then_prompt(text, settings)
        else:
            img = Image.open(io.BytesIO(image or b""))
            img.thumbnail((int(settings["max_edge"]),) * 2, Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=75)
            content = [{"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"}},
                       {"type": "text", "text": settings["prompt"]}]
        got = self._asked(content, settings, timeout=timeout)
        reply = got["reply"].strip()
        result: dict | str | None = None
        problem = ""
        if what == "read":
            result = self._text_read(reply)
        else:
            try:
                result = self._parse_description(reply)
            except Exception as e:  # noqa: BLE001 — what parsing it raised: it isn't what was asked for
                problem = f"Its answer isn't the description and tags asked for ({e})."
        if got["finish"] == "length":
            problem = out_of_room(int(settings["max_tokens"]))
        elif not reply:
            problem = "It answered nothing."
        return {**got, "result": result, "problem": problem, "limit": int(settings["max_tokens"])}
