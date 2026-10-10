"""Local CLI config: API URL and key, stored in ~/.lumiverb/config.json."""

import os
import tempfile
from pathlib import Path

from pydantic import BaseModel


class CLIConfig(BaseModel):
    """CLI configuration stored in ~/.lumiverb/config.json.

    Who this CLI is (the API and its key) and where things are on this
    machine. What changes the output (models, prompts, sizes) is the
    account's, on the server; processing is the scheduler's, with its own
    settings (src/server/scheduler/settings.py). Keys an older config file
    still has are ignored.
    """

    api_url: str = "http://localhost:8000"
    api_key: str = ""
    admin_key: str = ""
    # Library roots as stored on the server -> where they are on this
    # machine, by path prefix (src/processing/roots.py).
    root_map: dict[str, str] = {}
    # Where caches go instead of ~/.cache, when XDG_CACHE_HOME isn't set
    # (src/processing/cache_dir.py). The brain's install sets it to the data
    # disk, so a command run by hand as the lumiverb user finds the
    # scheduler's caches and lock.
    cache_home: str = ""


def _config_path() -> Path:
    return Path.home() / ".lumiverb" / "config.json"


class ConfigError(Exception):
    """The config file is there but can't be read. Never read as the defaults:
    the next save would wipe its keys and library roots."""


def load_config() -> CLIConfig:
    """Read config from file; defaults only if there's no file. A file that
    can't be read or parsed raises ConfigError."""
    path = _config_path()
    if not path.exists():
        return CLIConfig()
    try:  # it holds keys: its owner's only
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
    except OSError:
        pass  # not ours to change; reading it still works
    try:
        return CLIConfig.model_validate_json(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Can't read {path}: {exc}. Fix or move the file; nothing was changed.") from exc


def save_config(config: CLIConfig) -> None:
    """Write config to file, readable only by its owner (it holds keys), all at
    once: a temporary file beside it, renamed over it."""
    path = _config_path().resolve()  # a symlinked config stays a symlink
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:  # mkstemp makes it 0600
            f.write(config.model_dump_json(indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def get_api_url() -> str:
    """Return configured API base URL."""
    return load_config().api_url


def get_api_key() -> str:
    """Return configured API key."""
    return load_config().api_key


def get_admin_key() -> str:
    """Return configured admin key."""
    return load_config().admin_key


def machine_settings():
    """This machine's processing settings, as the CLI's config has them
    (src/processing/machine.py): where library roots are, and caches."""
    from src.processing.machine import Machine

    cfg = load_config()
    return Machine(root_map=dict(cfg.root_map), cache_home=cfg.cache_home)
