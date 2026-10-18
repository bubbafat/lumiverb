"""Lumiverb records and sends no telemetry.

FastAPI has OpenTelemetry built in (0.143+): request spans, metrics and logs,
exported when the environment says where (FASTAPI_OTEL_AUTO_CONFIGURE,
OTEL_EXPORTER_OTLP_ENDPOINT). The API turns every part of it off, whatever
the environment says, and nothing an install has (the Homebrew formula's
extras) brings OpenTelemetry's SDK or an exporter: fastapi[standard] would.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parent.parent
# What the Homebrew formula installs (uv sync --frozen --extra ...).
INSTALLED_EXTRAS = ("cli", "embeddings", "face_recognition", "workers")


def test_the_apis_telemetry_is_off_whatever_the_environment_says() -> None:
    env = {"FASTAPI_OTEL_AUTO_CONFIGURE": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
           "OTEL_TRACES_EXPORTER": "otlp"}
    code = ("import json; from src.server.api.main import app; "
            "print(json.dumps({k: v for k, v in app._telemetry.items() if isinstance(v, bool)}))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, check=True, capture_output=True, text=True,
                         env={**_inherited(), **env}).stdout
    switches = json.loads(out.strip().splitlines()[-1])
    assert switches == {"tracing": False, "metrics": False, "logs": False, "operation_spans": False,
                        "auto_configure": False}


def test_the_ai_workers_telemetry_is_off_too() -> None:
    env = {"FASTAPI_OTEL_AUTO_CONFIGURE": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"}
    code = ("import json; from src.server.ai_worker.app import make_app; app = make_app(None, 'k'); "
            "print(json.dumps({k: v for k, v in app._telemetry.items() if isinstance(v, bool)}))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, check=True, capture_output=True, text=True,
                         env={**_inherited(), **env}).stdout
    assert json.loads(out.strip().splitlines()[-1]) == {"tracing": False, "metrics": False, "logs": False,
                                                         "operation_spans": False, "auto_configure": False}


def _inherited() -> dict[str, str]:
    import os

    return dict(os.environ)


def _installed(lock: dict, extras: tuple[str, ...]) -> set[str]:
    """Every package an install with these extras gets, from uv.lock."""
    packages = {p["name"]: p for p in lock["package"]}
    wanted = [("lumiverb", extra) for extra in (None, *extras)]
    seen: set[tuple[str, str | None]] = set()
    while wanted:
        name, extra = wanted.pop()
        if (name, extra) in seen or name not in packages:
            continue
        seen.add((name, extra))
        package = packages[name]
        deps = package.get("dependencies", []) if extra is None else package.get("optional-dependencies", {}).get(extra, [])
        for dep in deps:
            wanted.append((dep["name"], None))
            wanted += [(dep["name"], e) for e in dep.get("extra", [])]
        if extra is not None:
            wanted.append((name, None))
    return {name for name, _ in seen}


def test_no_install_brings_an_opentelemetry_exporter() -> None:
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    installed = _installed(lock, INSTALLED_EXTRAS)
    assert {"fastapi", "starlette", "python-multipart", "torch"} <= installed  # the walk reaches the extras
    assert not {name for name in installed if name.startswith("opentelemetry-") and name != "opentelemetry-api"}
