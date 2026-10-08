"""Errors that carry a decision the request has to make.

When an action would surprise the user (deleting clips that projects
still use, restoring a project without saying what happens to its trashed
clips), the API refuses until the request states the choice. Every client
then has to show the user the facts and ask; a confirmation that lives in
one client only is a suggestion. The refusal is a 409 in the standard
envelope, with the facts in details.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ConflictError(Exception):
    """409 in the standard envelope: the request doesn't fit the thing's state
    (restoring an archived clip from the trash, say). code names the state."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class DecisionRequiredError(ConflictError):
    """409: the request must state the user's choice (see details)."""


class InvalidChoiceError(ConflictError):
    """422 in the standard envelope: the request asks for something that can't
    be done as asked (narrowing an all-or-nothing upgrade, say). code names it."""

    status_code = 422


class UpstreamError(ConflictError):
    """502 in the standard envelope: a service the request needed (the vision
    AI endpoint, say) didn't do its part. code names what; message says why."""

    status_code = 502


async def decision_required_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ConflictError)
    return JSONResponse(
        status_code=getattr(exc, "status_code", 409),
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )
