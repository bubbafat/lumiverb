"""Every route bounds what it takes, the same way (src/server/api/limits.py).

A `limit` past its bound is a 422, never a silent clamp, and a page of clips
holds at most MAX_PAGE everywhere. Every list of ids in a request body holds
at most MAX_IDS. Read from the OpenAPI schema, so a new route can't forget.
"""

from __future__ import annotations

import pytest

from src.server.api.limits import MAX_IDS, MAX_PAGE
from src.server.api.main import app

pytestmark = pytest.mark.fast


def _schema() -> dict:
    app.openapi_schema = None
    return app.openapi()


def _resolve(schema: dict, node: dict) -> dict:
    while "$ref" in node:
        node = schema["components"]["schemas"][node["$ref"].rsplit("/", 1)[1]]
    return node


def _limits():
    schema = _schema()
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            for p in op.get("parameters", []):
                if p["name"] == "limit" and p["in"] == "query":
                    yield f"{method.upper()} {path}", _resolve(schema, p["schema"])


def test_every_limit_says_its_bounds():
    unbounded = [where for where, s in _limits() if "maximum" not in s or "minimum" not in s]
    assert unbounded == []


# Not pages of clips: a library's reported changes are paths, read by the
# scheduler a report's worth (10,000) at a time.
NOT_CLIPS = {"GET /v1/libraries/{library_id}/changes"}


def test_no_page_of_clips_is_bigger_than_max_page():
    too_big = [(where, s["maximum"]) for where, s in _limits()
               if s.get("maximum", 0) > MAX_PAGE and where not in NOT_CLIPS]
    assert too_big == []


def _id_lists():
    schema = _schema()
    for name, model in schema["components"]["schemas"].items():
        for field, prop in (model.get("properties") or {}).items():
            if not field.endswith("_ids"):
                continue
            for option in prop.get("anyOf", [prop]):
                if option.get("type") == "array":
                    yield f"{name}.{field}", option


def test_every_id_list_in_a_body_is_capped_the_same():
    request_models = set()
    schema = _schema()
    for ops in schema["paths"].values():
        for op in ops.values():
            content = (op.get("requestBody") or {}).get("content", {})
            for media in content.values():
                ref = media.get("schema", {}).get("$ref")
                if ref:
                    request_models.add(ref.rsplit("/", 1)[1])
    wrong = [(where, option.get("maxItems")) for where, option in _id_lists()
             if where.split(".")[0] in request_models and option.get("maxItems") != MAX_IDS]
    assert wrong == []
