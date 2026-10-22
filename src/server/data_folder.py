"""What Lumiverb keeps in its data folder (DATA_DIR), by name: each folder
and file it makes there, now or later, and those it made there once (it
moves or removes what an install from before left). One list, so a folder
an admin chooses for previews (src/server/storage/move.py check_name) or
backups (src/server/backups.py check_name) never has one of these names:
Lumiverb would make it later, or move, empty or remove what's in it. Its
previews' account folders (ten_...) are the previews' own.

tests/test_data_folder.py holds the list to the code and the install
scripts: a new name in DATA_DIR joins it here."""

from __future__ import annotations

import os
from pathlib import Path

# Each name, and what it's for, as a refusal says it.
NAMES: dict[str, str] = {
    "ai-worker": "the AI worker's key and log",
    "backups": "its backups",
    "backups-state.json": "how its backups went",
    "backups.json": "where its backups go",
    "backups.lock": "its backups' lock",
    "cache": "its caches",
    "hf-cache": "AI models from before (moved into models as it starts)",
    "https-seen.json": "the devices that have used HTTPS",
    "insightface-cache": "the face model from before (moved into models as it starts)",
    "models": "its AI models",
    "network.json": "Settings › Network's choices",
    "nginx": "nginx's files",
    "plugins": "its plug-ins",
    "postgres": "its database",
    "quickwit": "the search index from before (removed as it starts)",
    "storage-move.json": "how a move of the previews is going",
    "storage-move.lock": "a move's lock",
    "storage.json": "where the previews are",
    "tls": "its certificates",
    "tmp": "the API's files in progress (emptied as it starts)",
    "upkeep-state.json": "when its upkeep last ran",
    "uploads": "uploads in progress",
    "worker-tmp": "the scheduler's work files (emptied as it starts)",
}

_FOLDED = {name.casefold(): what for name, what in NAMES.items()}


def kept(name: str) -> str | None:
    """What Lumiverb keeps under this name in DATA_DIR, or None. Matched
    whatever its case: a Mac's disk takes Models for models."""
    return _FOLDED.get(name.casefold())


def refusal(data_dir: Path, to: str | Path | None, *, own: str | None = None) -> str | None:
    """Why a folder chosen at `to` can't be one Lumiverb keeps something in,
    for its name in DATA_DIR (there yet or not, links followed), or None.
    `own`: the name that's the chooser's own (backups for the backups
    folder). Says nothing of a path that isn't a full one: the chooser's
    own check says what's wrong with it."""
    raw = str(to or "").strip()
    if not raw or not Path(raw).expanduser().is_absolute():
        return None
    data = Path(os.path.realpath(data_dir))
    real = Path(os.path.realpath(Path(raw).expanduser()))
    if real == data or not real.is_relative_to(data):
        return None
    name = real.relative_to(data).parts[0]
    if (what := kept(name)) is None or (own is not None and name.casefold() == own.casefold()):
        return None
    return (f"In Lumiverb's data folder ({data_dir}), the name {name} is its own, for {what}: choose another name, "
            "or a folder outside it.")
