"""Logging configuration for Lumiverb CLI and workers."""

from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    """Configure root logger based on LOG_LEVEL env var (default INFO)."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

