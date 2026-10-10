"""Every error is one envelope: {"error": {"code", "message", "details"}}.

A plain HTTPException gets the code its status names, a request that fails
validation is a 422 invalid_request with the errors in details, an ApiError
says its own code, and an unhandled error is a 500 internal_error. No route
builds the envelope by hand inside an HTTPException's detail.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from src.server.api.main import app

pytestmark = pytest.mark.fast

SERVER = Path(__file__).resolve().parents[1] / "src" / "server"


def _envelope(r) -> dict:
    body = r.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message", "details"}, body
    assert isinstance(body["error"]["message"], str)
    return body["error"]


def test_a_plain_http_exception_is_an_envelope():
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/v1/upkeep")  # no key: the upkeep scope's HTTPException(401)
    assert r.status_code == 401
    assert _envelope(r)["code"] == "unauthorized"


def test_an_unknown_route_is_an_envelope():
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/v1/filters/nothing-here")  # skips the tenant middleware
    assert r.status_code == 404
    assert _envelope(r)["code"] == "not_found"


def test_a_validation_error_is_an_envelope():
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/v1/auth/login", json={"email": 5})
    assert r.status_code == 422
    err = _envelope(r)
    assert err["code"] == "invalid_request"
    assert err["details"]["errors"]
    assert err["message"].startswith(("email", "password"))


def _toy() -> TestClient:
    from src.server.api.errors import (
        ApiError,
        api_error_handler,
        http_exception_handler,
        unhandled_error_handler,
        validation_error_handler,
    )
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    toy = FastAPI()
    toy.add_exception_handler(ApiError, api_error_handler)
    toy.add_exception_handler(StarletteHTTPException, http_exception_handler)
    toy.add_exception_handler(RequestValidationError, validation_error_handler)
    toy.add_exception_handler(Exception, unhandled_error_handler)

    class Body(BaseModel):
        x: float = Field(le=10)

    @toy.post("/echo")
    def echo(body: Body) -> dict:
        return {"x": body.x if math.isfinite(body.x) else None}

    @toy.get("/teapot")
    def teapot() -> None:
        raise ApiError(503, "busy", "Busy.", {"n": 1}, headers={"Retry-After": "2"})

    @toy.get("/plain")
    def plain() -> None:
        raise HTTPException(status_code=409, detail="Taken", headers={"X-Why": "taken"})

    @toy.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    return TestClient(toy, raise_server_exceptions=False)


def test_api_errors_keep_their_code_details_and_headers():
    r = _toy().get("/teapot")
    assert r.status_code == 503 and r.headers["retry-after"] == "2"
    assert _envelope(r) == {"code": "busy", "message": "Busy.", "details": {"n": 1}}


def test_http_exception_headers_survive():
    r = _toy().get("/plain")
    assert r.status_code == 409 and r.headers["x-why"] == "taken"
    assert _envelope(r) == {"code": "conflict", "message": "Taken", "details": {}}


def test_validation_of_inf_or_nan_still_answers():
    r = _toy().post("/echo", content=b'{"x": 1e999}', headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and _envelope(r)["code"] == "invalid_request"


def test_an_unhandled_error_is_a_500_envelope():
    r = _toy().get("/boom")
    assert r.status_code == 500
    assert _envelope(r)["code"] == "internal_error"


def test_no_route_nests_the_envelope_in_detail():
    """HTTPException(detail=...) takes a message; codes and details go through ApiError."""
    offenders = []
    for path in SERVER.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "HTTPException":
                for kw in node.keywords:
                    if kw.arg == "detail" and isinstance(kw.value, (ast.Dict, ast.Call)) and not (
                        isinstance(kw.value, ast.Call) and getattr(kw.value.func, "id", None) == "str"
                    ):
                        offenders.append(f"{path.relative_to(SERVER)}:{node.lineno}")
    assert offenders == []
