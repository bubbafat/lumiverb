"""Lumiverb's AI worker (ADR-022, src/server/ai_worker/), as the scheduler
calls it: an image's CLIP vector; a job's photos' faces.

A machine's URL is as Settings › AI keeps every AI machine's (…/v1, its
models at …/v1/models), so its own routes are …/v1/lumiverb/clip and
…/v1/lumiverb/faces. Whose a failure is: the machine's (unreachable, its key
refused, the model not served, out of memory, any 5xx: the work goes to
another machine) or the image's (one it can't read, which it says per
image; a request it refuses as too big). Its address is checked at each
request and no redirect is followed (src/shared/ai_requests.py), and over
HTTPS its certificate is the one it was added with (its fingerprint); what it
says when it fails is the log's, the failure in Lumiverb's words.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from src.processing.workers.embeddings.base import EmbeddingError
from src.processing.workers.faces import FaceError
from src.shared import ai_requests
from src.shared.ai_requests import LOGGED_UP_TO
from src.shared.vision_endpoint import VisionEndpointError, check_url

logger = logging.getLogger(__name__)

# One image's vector takes well under a second on a GPU, a few on a CPU; a
# job's photos' faces longer. A machine that doesn't answer in this long is down.
TIMEOUT_SEC = 120.0


class AiWorker:
    def __init__(self, api_url: str, api_key: str | None, *, fingerprint: str = "", timeout: float = TIMEOUT_SEC,
                 error: type[EmbeddingError] | type[FaceError] = EmbeddingError) -> None:
        """fingerprint: the certificate it was added with (an https worker's). error: what the caller's job says a
        machine's failure is (CLIP's, faces')."""
        try:
            self._url = check_url(api_url)
        except VisionEndpointError as e:
            raise error(str(e), endpoint_fault=True) from None
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._fingerprint = fingerprint
        self._timeout = timeout

    def _post(self, route: str, data: dict[str, str], images: list[bytes], error: type[Exception]) -> dict[str, Any]:
        files = [("images", (f"{i}.img", image, "application/octet-stream")) for i, image in enumerate(images)]
        try:
            # Its address checked, no redirect followed: the answer is the machine's own (and its key goes
            # nowhere else).
            r = ai_requests.post(f"{self._url}/lumiverb/{route}", headers=self._headers, data=data, files=files,
                                 timeout=self._timeout, fingerprint=self._fingerprint)
        except (ai_requests.AddressRefused, ai_requests.NotItsCertificate, ai_requests.Redirected) as e:
            raise error(str(e), endpoint_fault=True) from None
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
        # A request too big, or one it can't read, is what was sent; everything else is the machine. Said in
        # Lumiverb's words: what it said is the log's.
        mine = r.status_code in (400, 413, 422)
        raise error(f"{self._url} {ai_requests.answered(r, model=data.get('model', ''), api='Lumiverb AI worker')}",
                    endpoint_fault=not mine)

    def _image_error(self, said: object, error: type[EmbeddingError] | type[FaceError]) -> Exception:
        """An image it couldn't read: the image's, said in Lumiverb's words (what it said is the log's)."""
        logger.warning("%s couldn't read an image: %s", self._url, " ".join(str(said).split())[:LOGGED_UP_TO])
        return error(f"{self._url} couldn't read the image", endpoint_fault=False)

    def clip(self, image: bytes, model: str) -> dict[str, Any]:
        """{vector, model_id, model_version} for one image. EmbeddingError says whose a failure is."""
        body = self._post("clip", {"model": model}, [image], EmbeddingError)
        results = body.get("results")
        result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
        if "error" in result:
            raise self._image_error(result["error"], EmbeddingError)
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
            raise self._image_error(result["error"], FaceError)
        if not isinstance(result.get("faces"), list):
            raise FaceError(f"{self._url} gave no faces list", endpoint_fault=True)
        return result

