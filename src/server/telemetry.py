"""FastAPI's own OpenTelemetry, off whatever the environment says
(FASTAPI_OTEL_AUTO_CONFIGURE, OTEL_*): Lumiverb records and sends nothing.
Every FastAPI app Lumiverb runs is made with it: the API's
(src/server/api/main.py) and the AI worker's (src/server/ai_worker/app.py).
"""

NO_TELEMETRY = {"tracing": False, "metrics": False, "logs": False, "operation_spans": False,
                "auto_configure": False}
