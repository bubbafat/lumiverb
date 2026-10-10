"""Every error the API sends is one envelope:
{"error": {"code", "message", "details"}}.

`ApiError(status, code, message, details)` raises one directly. A plain
`HTTPException(status, "message")` gets the code its status names
(`http_exception_handler`), and a request that fails validation is a 422
`invalid_request` with the errors in details.

Errors that carry a decision the request has to make: when an action would
surprise the user (deleting clips that projects still use, restoring a
project without saying what happens to its trashed clips), the API refuses
until the request states the choice. Every client then has to show the user
the facts and ask; a confirmation that lives in one client only is a
suggestion. The refusal is a 409 in the standard envelope, with the facts in
details.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    """An error in the standard envelope with any status. code names it."""

    status_code = 500

    def __init__(
        self, status_code: int | None = None, code: str = "error", message: str = "",
        details: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers


class ConflictError(ApiError):
    """409: the request doesn't fit the thing's state (restoring an archived
    clip from the trash, say). code names the state."""

    status_code = 409

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(None, code, message, details)


class DecisionRequiredError(ConflictError):
    """409: the request must state the user's choice (see details)."""


class InvalidChoiceError(ConflictError):
    """422: the request asks for something that can't be done as asked
    (narrowing an all-or-nothing upgrade, say). code names it."""

    status_code = 422


class UpstreamError(ConflictError):
    """502: a service the request needed (the vision AI endpoint, say) didn't
    do its part. code names what; message says why."""

    status_code = 502


def envelope(status_code: int, code: str, message: str, details: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
        headers=headers,
    )


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return envelope(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


# The code a plain HTTPException gets from its status.
STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    416: "range_not_satisfiable",
    422: "invalid_request",
    500: "internal_error",
    502: "upstream_error",
    503: "unavailable",
}


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return envelope(exc.status_code, STATUS_CODES.get(exc.status_code, "error"), message,
                    headers=getattr(exc, "headers", None))


def _finite(value: Any) -> Any:
    """The rejected input may hold inf or NaN, which isn't valid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = _finite(jsonable_encoder(exc.errors()))
    first = errors[0] if errors else {}
    where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
    msg = str(first.get("msg", "Invalid request"))
    return envelope(422, "invalid_request", f"{where}: {msg}" if where else msg, {"errors": errors})


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """A 500 in the envelope. Starlette still logs the exception (and re-raises it to the server)."""
    return envelope(500, "internal_error", "Something went wrong on the server.")
