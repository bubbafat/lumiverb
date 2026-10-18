"""The AI worker's API (ADR-022): what another machine's scheduler asks.

GET  /v1/models            the models it serves, as every AI machine lists them (OpenAI's shape)
POST /v1/lumiverb/clip     images and a CLIP model -> one vector each
POST /v1/lumiverb/faces    images and the face settings -> each one's faces

Every request carries its key (Authorization: Bearer). Images are JPEG, PNG
or WebP of at most 120 megapixels, checked before any model sees them (the
server's rules, src/server/images.py); one that isn't is that image's error,
never the request's. The work takes turns, at_once requests at a time. A
request that fails for the machine's sake (out of memory, a model that
won't load, the face process dying) is a 503: the scheduler sends its images
to another machine.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

logger = logging.getLogger(__name__)

# The CLIP models it serves, by the name the account's job knows each by -> open_clip's (model, pretrained).
CLIP_MODELS: dict[str, tuple[str, str]] = {"ViT-B-32/openai": ("ViT-B-32", "openai")}
# The face models it serves (InsightFace's model pack).
FACE_MODELS = frozenset({"buffalo_l"})
# What one request may carry: a job's photos (faces: 25) with room to spare,
# each a preview or a video's frame (a few MB at most).
MAX_IMAGES = 32
MAX_IMAGE_BYTES = 40 * 1024 * 1024
# What a faces request's settings may be, as JSON.
MAX_SETTINGS_BYTES = 16 * 1024


class _RefusedError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def make_app(models: Any, key: str, *, at_once: int = 2) -> FastAPI:
    """The worker's app: its models (src/server/scheduler/models.py Models), its key, its turns."""
    from src.server.telemetry import NO_TELEMETRY

    app = FastAPI(title="Lumiverb AI worker", telemetry=NO_TELEMETRY, docs_url=None, redoc_url=None,
                  openapi_url=None)
    turns = threading.BoundedSemaphore(max(1, at_once))
    expected = f"Bearer {key}".encode()

    @app.middleware("http")
    async def _keyed(request: Request, call_next):
        given = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(given, expected):
            return _error(401, "unauthorized", "This AI worker's key is needed (Authorization: Bearer).")
        return await call_next(request)

    @app.exception_handler(_RefusedError)
    async def _refused(_request: Request, exc: _RefusedError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.get("/v1/models")
    def list_models() -> dict:
        ids = [*CLIP_MODELS, *sorted(FACE_MODELS)]
        return {"object": "list", "data": [{"id": name, "object": "model", "owned_by": "lumiverb"} for name in ids]}

    async def _images(request: Request) -> tuple[Any, list[bytes]]:
        try:
            form = await request.form(max_files=MAX_IMAGES, max_fields=8, max_part_size=MAX_SETTINGS_BYTES)
        except Exception as exc:  # noqa: BLE001 — too many files, a field too big, not a form
            raise _RefusedError(400, "bad_request", f"Not a form it reads: {exc}") from None
        files = [f for f in form.getlist("images") if isinstance(f, UploadFile)]
        if not files:
            raise _RefusedError(400, "bad_request", "Send the images as `images`.")
        out = []
        for f in files:
            data = await f.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise _RefusedError(413, "too_large", f"An image is bigger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
            out.append(data)
        return form, out

    def _in_turn(work):
        with turns:
            return work()

    def _machines_fault(exc: Exception) -> _RefusedError:
        from src.producers.runner import out_of_memory

        if out_of_memory(exc):
            return _RefusedError(503, "out_of_memory", f"Out of memory: {exc}")
        logger.exception("AI worker: a request failed")
        return _RefusedError(503, "unavailable", f"The worker couldn't do it: {exc or type(exc).__name__}")

    @app.post("/v1/lumiverb/clip")
    async def clip(request: Request) -> dict:
        form, images = await _images(request)
        model = str(form.get("model") or "")
        if model not in CLIP_MODELS:
            raise _RefusedError(404, "model_not_found", f"This worker doesn't serve CLIP model {model!r}.")

        def work() -> dict:
            from src.server.images import NotAnImageError, rgb

            provider = models.clip(*CLIP_MODELS[model])
            results: list[dict] = []
            for data in images:
                try:
                    img = rgb(data)
                except NotAnImageError as exc:
                    results.append({"error": f"The image {exc}."})
                    continue
                try:
                    results.append({"vector": provider.embed_image(img)})
                finally:
                    img.close()
            return {"model_id": provider.model_id, "model_version": provider.model_version, "results": results}

        try:
            return await run_in_threadpool(_in_turn, work)
        except _RefusedError:
            raise
        except Exception as exc:  # noqa: BLE001 — the model, the GPU: the machine's
            raise _machines_fault(exc) from None

    @app.post("/v1/lumiverb/faces")
    async def faces(request: Request) -> dict:
        form, images = await _images(request)
        try:
            settings = json.loads(str(form.get("settings") or "{}"))
        except ValueError:
            raise _RefusedError(400, "bad_request", "`settings` is JSON: the face settings and the level's gates.") from None
        if not isinstance(settings, dict):
            raise _RefusedError(400, "bad_request", "`settings` is a JSON object.")
        if settings.get("model") not in FACE_MODELS:
            raise _RefusedError(404, "model_not_found", f"This worker doesn't serve face model {settings.get('model')!r}.")

        def work() -> dict:
            from src.producers.runner import out_of_memory
            from src.server.images import NotAnImageError, opened

            detector = models.faces()
            results: list[dict] = []
            for data in images:
                try:
                    opened(data).close()  # its format and size, before the face process reads it
                except NotAnImageError as exc:
                    results.append({"error": f"The image {exc}."})
                    continue
                found = detector.detect(data, settings)
                if "error" in found and out_of_memory(found["error"]):
                    raise RuntimeError(found["error"])  # the GPU's: the request goes elsewhere
                results.append(found)
            return {"results": results}

        try:
            return await run_in_threadpool(_in_turn, work)
        except _RefusedError:
            raise
        except Exception as exc:  # noqa: BLE001 — the face process died, the GPU: the machine's
            raise _machines_fault(exc) from None

    return app
