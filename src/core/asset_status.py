"""Asset status constants for pipeline stages and UI."""

PENDING: str = "pending"
PROXY_READY: str = "proxy_ready"
DESCRIBED: str = "described"
INDEXED: str = "indexed"

"""Canonical asset status constants for the ingestion pipeline."""

STATUS_PENDING = "pending"         # just ingested, no proxy yet
STATUS_PROXY_READY = "proxy_ready" # thumbnail + proxy available, viewable
STATUS_DESCRIBED = "described"     # AI description complete
STATUS_INDEXED = "indexed"         # in Quickwit, fully searchable

