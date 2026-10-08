"""Local CLI config: API URL and key, stored in ~/.lumiverb/config.json."""

from pathlib import Path

from pydantic import BaseModel


class CLIConfig(BaseModel):
    """CLI configuration stored in ~/.lumiverb/config.json.

    Only how this machine works (where things are, how much at once). What
    changes the output (models, prompts, sizes, the vision AI endpoint) is
    the account's, on the server: one source of truth. Keys an older config
    file still has are ignored.
    """

    api_url: str = "http://localhost:8000"
    api_key: str = ""
    admin_key: str = ""
    face_batch_size: int = 25
    face_batch_limit: int = 20
    max_concurrency: int = 4
    vision_concurrency: int = 2
    ocr_concurrency: int = 1
    ocr_batch_size: int = 25
    transcribe_concurrency: int = 1
    # Library roots as stored on the server -> where they are on this
    # machine, by path prefix (src/client/cli/roots.py).
    root_map: dict[str, str] = {}
    # Analysis proxies (src/client/video/analysis_proxy.py) and their cache.
    # The encoder is this machine's way of rendering (it doesn't change what's tracked).
    analysis_proxy_encoder: str = "libx264"
    analysis_cache_gb: float = 50.0
    # Where caches, and the worker's lock and state, go instead of ~/.cache,
    # when XDG_CACHE_HOME isn't set (src/client/cache_dir.py). The brain's
    # install sets it to the data disk, so a manual `worker --once` finds the
    # service's lock.
    cache_home: str = ""


def _config_path() -> Path:
    return Path.home() / ".lumiverb" / "config.json"


def load_config() -> CLIConfig:
    """Read config from file; return defaults if file is missing."""
    path = _config_path()
    if not path.exists():
        return CLIConfig()
    try:
        data = path.read_text()
        return CLIConfig.model_validate_json(data)
    except Exception:
        return CLIConfig()


def save_config(config: CLIConfig) -> None:
    """Write config to file; create directory if needed."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.model_dump_json(indent=2))


def get_api_url() -> str:
    """Return configured API base URL."""
    return load_config().api_url


def get_api_key() -> str:
    """Return configured API key."""
    return load_config().api_key


def get_admin_key() -> str:
    """Return configured admin key."""
    return load_config().admin_key
