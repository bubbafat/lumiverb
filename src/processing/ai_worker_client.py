"""Lumiverb's AI worker (ADR-022, src/server/ai_worker/), as the scheduler
calls it: an image's CLIP vector; a job's photos' faces.

A machine's URL is as Settings › AI keeps every AI machine's (…/v1, its
models at …/v1/models), so its own routes are …/v1/lumiverb/clip and
…/v1/lumiverb/faces. Whose a failure is: the machine's (unreachable, its key
refused, the model not served, out of memory, any 5xx: the work goes to
another machine) or the image's (one it can't read, which it says per
image; a request it refuses as too big).
"""

from __future__ import annotations

from typing import Any

import requests

from src.processing.workers.embeddings.base import EmbeddingError
from src.processing.workers.faces import FaceError
from src.shared.vision_endpoint import VisionEndpointError, check_url

# One image's vector takes well under a second on a GPU, a few on a CPU; a
# job's photos' faces longer. A machine that doesn't answer in this long is down.
TIMEOUT_SEC = 120.0


class AiWorker:
    def __init__(self, api_url: str, api_key: str | None, *, timeout: float = TIMEOUT_SEC,
                 error: type[EmbeddingError] | type[FaceError] = EmbeddingError) -> None:
        """error: what the caller's job says a machine's failure is (CLIP's, faces')."""
        try:
            self._url = check_url(api_url)
        except VisionEndpointError as e:
            raise error(str(e), endpoint_fault=True) from None
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout

    def _post(self, route: str, data: dict[str, str], images: list[bytes], error: type[Exception]) -> dict[str, Any]:
        files = [("images", (f"{i}.img", image, "application/octet-stream")) for i, image in enumerate(images)]
        try:
            # No redirects: the answer is the machine's own (and its key goes nowhere else).
            r = requests.post(f"{self._url}/lumiverb/{route}", headers=self._headers, data=data, files=files,
                              timeout=self._timeout, allow_redirects=False)
        except requests.RequestException as e:
            raise error(f"{self._url} didn't answer ({type(e).__name__})", endpoint_fault=True) from None
        if r.status_code == 200:
            try:
                body = r.json()
            except ValueError:
                raise error(f"{self._url} answered with something that isn't JSON", endpoint_fault=True) from None
            if isinstance(body, dict):
                return body
            raise error(f"{self._url} answered with something it doesn't say", endpoint_fault=True)
        try:
            said = (r.json().get("error") or {}).get("message") or r.text[:200]
        except (ValueError, AttributeError):
            said = r.text[:200]
        # A request too big, or one it can't read, is what was sent; everything else is the machine.
        mine = r.status_code in (400, 413, 422)
        raise error(f"{self._url}: HTTP {r.status_code}: {said}", endpoint_fault=not mine)

    def clip(self, image: bytes, model: str) -> dict[str, Any]:
        """{vector, model_id, model_version} for one image. EmbeddingError says whose a failure is."""
        body = self._post("clip", {"model": model}, [image], EmbeddingError)
        results = body.get("results")
        result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
        if "error" in result:
            raise EmbeddingError(str(result["error"]), endpoint_fault=False)
        vector = result.get("vector")
        if not isinstance(vector, list) or not vector:
            raise EmbeddingError(f"{self._url} gave no vector", endpoint_fault=True)
        return {"vector": vector, "model_id": body.get("model_id"), "model_version": body.get("model_version")}

    def faces(self, image: bytes, settings: dict[str, Any]) -> dict[str, Any]:
        """What it found in one image, as the scheduler's face process says it. FaceError says whose a failure is."""
        import json

        body = self._post("faces", {"settings": json.dumps(settings)}, [image], FaceError)
        results = body.get("results")
        result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
        if result is None:
            raise FaceError(f"{self._url} found nothing it says", endpoint_fault=True)
        if "error" in result:
            raise FaceError(str(result["error"]), endpoint_fault=False)
        if not isinstance(result.get("faces"), list):
            raise FaceError(f"{self._url} gave no faces list", endpoint_fault=True)
        return result

