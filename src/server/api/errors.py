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


class GoneError(ApiError):
    """410 gone: a retired route. It stays defined, behind the role it always
    needed, so a caller still using it is told what replaced it (the message,
    and details.replaced_by) instead of a bare 404."""

    status_code = 410

    def __init__(self, message: str, replaced_by: list[str]) -> None:
        super().__init__(None, "gone", message, {"replaced_by": replaced_by})


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
    410: "gone",
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


def _answerable(value: Any) -> Any:
    """The rejected input as JSON can carry it: inf or NaN as text, and a body
    that isn't text (a file sent where JSON was asked for) as its size."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, bytes):
        try:
            return value.decode()
        except UnicodeDecodeError:
            return f"{len(value)} bytes that aren't text"
    if isinstance(value, dict):
        return {k: _answerable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_answerable(v) for v in value]
    return value


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = jsonable_encoder(_answerable(exc.errors()))
    first = errors[0] if errors else {}
    where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
    msg = str(first.get("msg", "Invalid request"))
    return envelope(422, "invalid_request", f"{where}: {msg}" if where else msg, {"errors": errors})


# Said to whoever asked, however they're signed in: no folder named.
STORAGE_UNAVAILABLE = ("Where Lumiverb keeps its previews can't be reached now (is its drive plugged in?). "
                       "Try again when it's back.")


async def storage_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """The Store couldn't reach where a key is kept (src/server/storage/store.py
    Unavailable): 503, to try again, never a 404 (nothing is missing). The
    folder is the log's, not the answer's."""
    import logging

    logging.getLogger(__name__).warning("%s %s: %s", request.method, request.url.path, exc)
    return envelope(503, "storage_unavailable", STORAGE_UNAVAILABLE, headers={"Retry-After": "30"})


# Text that isn't text in what a request sent (src/server/api/no_nul.py): PostgreSQL keeps none of it. What was
# found, details.found, says which.
NOT_TEXT = "not_text"
NOT_TEXT_SAYS = {
    "nul": "Nothing sent to Lumiverb can hold a NUL character (\\0): send it without one.",
    "surrogate": ("Nothing sent to Lumiverb can hold half a character (a lone surrogate, U+D800 to U+DFFF): "
                  "send the whole character, or none."),
    "not_utf8": "A URL's text is UTF-8, its other bytes percent-encoded: this one has bytes that aren't UTF-8.",
}
# psycopg2's refusal of text holding a NUL, a ValueError: "A string literal cannot contain NUL (0x00) characters."
_DATABASE_NUL = "NUL (0x00)"


def not_text(found: str, where: str | None = None) -> ApiError:
    """400 not_text: details.found says what (nul, surrogate or not_utf8), details.in where (path, query, body or
    form) when it's known."""
    return ApiError(400, NOT_TEXT, NOT_TEXT_SAYS[found], {"found": found, **({"in": where} if where else {})})


def not_text_response(found: str, where: str | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    error = not_text(found, where)
    return envelope(error.status_code, error.code, error.message, error.details, headers)


def refused_by_the_database(exc: Exception) -> str | None:
    """What the database refused of a request's text, or None: "nul" (psycopg2's
    ValueError; jsonb's 22P05 for one written \\u0000) or "surrogate" (psycopg2
    encoding one, a UnicodeEncodeError raised in its or SQLAlchemy's code, never
    a response's own; jsonb's 22P02 for one written \\ud800)."""
    if type(exc) is ValueError:
        return "nul" if _DATABASE_NUL in str(exc) else None
    if isinstance(exc, UnicodeEncodeError):
        return "surrogate" if exc.reason == "surrogates not allowed" and _raised_sending_a_query(exc) else None
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None)
    if code == "22P05":  # untranslatable_character: "\u0000 cannot be converted to text."
        return "nul"
    if code == "22P02" and "surrogate" in str(orig):  # "Unicode low surrogate must follow a high surrogate."
        return "surrogate"
    return None


def _raised_sending_a_query(exc: BaseException) -> bool:
    """Whether the innermost Python code the exception passed was the database layer's (SQLAlchemy's execute,
    calling psycopg2's C code, or psycopg2's own)."""
    module, tb = "", exc.__traceback__
    while tb is not None:
        module, tb = tb.tb_frame.f_globals.get("__name__", ""), tb.tb_next
    return module.split(".", 1)[0] in ("sqlalchemy", "psycopg2")


async def not_text_handler(request: Request, exc: Exception) -> JSONResponse:
    """A NUL or a lone surrogate that reached a query past no_nul.py's gates:
    the same 400, never a 500, logged so the gate that missed it can be found.
    Any other ValueError or DataError is raised on, a 500 as ever."""
    import logging

    found = refused_by_the_database(exc)
    if found is None:
        raise exc
    logging.getLogger(__name__).warning("%s %s: the database refused a request's text (%s)",
                                        request.method, request.url.path, found)
    return not_text_response(found)


async def bad_cursor_handler(request: Request, exc: Exception) -> JSONResponse:
    """A cursor that isn't one the list gave (src/server/cursors.py): 400 bad_cursor, whichever list read it."""
    return envelope(400, "bad_cursor", str(exc))


async def secrets_key_missing_handler(request: Request, exc: Exception) -> JSONResponse:
    """A secret to seal (an AI machine's key, a plug-in's secret setting) on an install with no
    LUMIVERB_SECRETS_KEY (src/server/sealing.py): 503, saying how it gets one; nothing is kept unsealed."""
    return envelope(503, "secrets_key_missing", str(exc))


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """A 500 in the envelope. Starlette still logs the exception (and re-raises it to the server)."""
    return envelope(500, "internal_error", "Something went wrong on the server.")
