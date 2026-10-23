"""Text that isn't text in anything a request sends is a 400 not_text, never
a 500.

PostgreSQL keeps text, and two things a string can hold aren't: a NUL
character (psycopg2 won't send one, a ValueError; jsonb refuses one written
\\u0000) and a lone surrogate, half of a UTF-16 pair and no character at all
(it has no UTF-8 for psycopg2 to send, a UnicodeEncodeError; jsonb refuses
one written \\ud800). Either one that reached a query was the database's
error, a 500. No text Lumiverb keeps can hold one, so nothing is lost by
refusing it once, before any route. details.found says what was found
(nul, surrogate, or not_utf8: a URL whose bytes, percent-decoded, aren't
UTF-8), details.in where:

- In the URL, by NoNul: the path (its bytes as sent, and as the routes
  match it) and every query parameter (its bytes, and as the routes read
  it). A middleware inside the exposure gate (exposure.py: a route a server
  doesn't expose is a 404 still, whatever it holds) and outside sign-in
  (middleware.py), so the public pages' lookups, which take an id from the
  URL to the control database, never see one. Only the URL is read.
- In a body, by `gate`, the app's first dependency (main.py): after sign-in
  and the demo's gate, so no body is read for a request they turn away, and
  before the device and library gates, which look up the ids a body names.
  A JSON body's strings, keys too, and a form's text fields (urlencoded or
  multipart), as FastAPI read them for the route; a JSON or urlencoded body
  a route reads itself is read here first, as it comes. Text that is a JSON
  object or array itself (ingest's exif, a lineage: a route decodes it) is
  read as JSON too. Nothing else is read: a file in a form is never opened,
  and chunks, previews and videos are bytes. A form's bytes that aren't
  UTF-8 are Starlette's to read (as Latin-1, or U+FFFD): text either way.

A NUL or surrogate that slips past is still the same 400 (errors.py
not_text_handler): a body a route reads whatever it's called (the batch
rating's), sent as something other than JSON, say.
"""

from __future__ import annotations

import json
import re
from contextlib import suppress
from typing import Any
from urllib.parse import unquote_to_bytes

from fastapi import params
from starlette.datastructures import FormData, QueryParams
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from src.server.api.errors import not_text, not_text_response

NUL = "\x00"
# Where a JSON body may hold one: a NUL or a surrogate as JSON writes it (\u0000, \ud800 to \udfff; a NUL
# itself is no JSON), a surrogate's own bytes (JSON as Python reads it takes them), or NUL bytes (a body
# that isn't UTF-8, UTF-16's, has them). Only then is it walked.
_MAY_HOLD = re.compile(rb"\\u(?:0000|[dD][89a-fA-F])|\x00|\xed[\xa0-\xbf]")
# Text that, read as JSON, writes one.
_WRITES = re.compile(r"\\u(?:0000|[dD][89a-fA-F])")


class NoNul:
    """The path and every query parameter: text that isn't text is a 400 before anything else reads it."""

    def __init__(self, app: ASGIApp, headers: dict[str, str]) -> None:
        self.app = app
        self.headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        refused = in_url(scope) if scope["type"] == "http" else None
        if refused is not None:
            where, found = refused
            await not_text_response(found, where, headers=self.headers)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def in_url(scope: Scope) -> tuple[str, str] | None:
    """(where, found) for text in the URL that isn't text, or None: "path" or
    "query"; "not_utf8" for bytes that, percent-decoded, aren't UTF-8 (which
    the routes would read as U+FFFD), else "nul" or "surrogate"."""
    raw_path = scope.get("raw_path")
    if raw_path is not None and not _utf8(raw_path):
        return "path", "not_utf8"
    if found := found_in(scope["path"]):
        return "path", found
    query = scope.get("query_string", b"")
    if query:
        if not _utf8(query):
            return "query", "not_utf8"
        for name, value in QueryParams(query).multi_items():
            if found := found_in(name) or found_in(value):
                return "query", found
    return None


def _utf8(raw: bytes) -> bool:
    """Whether a URL's bytes, percent-decoded, are UTF-8 (a surrogate's bytes aren't)."""
    try:
        unquote_to_bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def found_in(text: str) -> str | None:
    """"nul" or "surrogate" when the text holds one, else None."""
    if NUL in text:
        return "nul"
    if not text.isascii():
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return "surrogate"
    return None


async def gate(request: Request) -> None:
    """Before every route: a body holding text that isn't text is a 400 (see the module's docstring)."""
    kind = _kind(request.headers.get("content-type"))
    takes_a_form = _takes_a_form(request)
    if takes_a_form or kind == "form":
        if not takes_a_form:
            await request.body()  # kept: a route that reads its body itself still can
        if found := _found_in_form(await request.form()):  # FastAPI's, for a route that takes one: never read again
            raise not_text(found, "form")
    if kind == "json":
        if not _MAY_HOLD.search(await request.body()):
            return
        try:
            value = await request.json()
        except (ValueError, RecursionError):
            return  # not JSON: the route refuses it as ever
        if found := _found(value):
            raise not_text(found, "body")


def _kind(content_type: str | None) -> str | None:
    """"json" for a JSON body (application/json, or application/…+json, as
    FastAPI reads one), "form" for a urlencoded one, else None: not read."""
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media == "application/json" or media.startswith("application/") and media.endswith("+json"):
        return "json"
    if media == "application/x-www-form-urlencoded":
        return "form"
    return None


def _takes_a_form(request: Request) -> bool:
    """Whether the route takes a form: FastAPI has read it (urlencoded or multipart) before any dependency runs."""
    field = getattr(request.scope.get("route"), "body_field", None)
    return field is not None and isinstance(field.field_info, params.Form)


def _found_in_form(form: FormData) -> str | None:
    """A form's text fields and their names; a file is its own (UploadFile: never opened here)."""
    for name, value in form.multi_items():
        if found := found_in(name) or (_found(value) if isinstance(value, str) else None):
            return found
    return None


def _found(value: Any) -> str | None:
    """"nul" or "surrogate" when any string in a JSON value, a key or a value
    at any depth, holds one; a string that is a JSON object or array itself
    and writes one, read as JSON."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if found := found_in(item):
                return found
            if _WRITES.search(item) and item.lstrip()[:1] in ("{", "["):
                with suppress(ValueError, RecursionError):  # else text, as it reads
                    stack.append(json.loads(item))
        elif isinstance(item, dict):
            for key, inner in item.items():
                stack.extend((key, inner))
        elif isinstance(item, list):
            stack.extend(item)
    return None
