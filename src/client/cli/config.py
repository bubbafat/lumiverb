"""Local CLI config: API URL and key, stored in ~/.lumiverb/config.json."""

from pathlib import Path

from pydantic import BaseModel


class CLIConfig(BaseModel):
    """CLI configuration stored in ~/.lumiverb/config.json.

    Only how this machine works (where things are, how much at once). What
    changes the output (models, prompts, sizes) is the account's, on the
    server: one source of truth, and so are its AI machines (Settings → AI).
    Keys an older config file still has are ignored.
    """

    api_url: str = "http://localhost:8000"
    api_key: str = ""
    admin_key: str = ""
    face_batch_size: int = 25
    face_batch_limit: int = 20
    max_concurrency: int = 4
    # How many vision and transcription requests go at once is each AI machine's (Settings → AI).
    ocr_batch_size: int = 25
    # Library roots as stored on the server -> where they are on this
    # machine, by path prefix (src/client/cli/roots.py).
    root_map: dict[str, str] = {}
    # Analysis proxies (src/client/video/analysis_proxy.py) and their cache.
    # The encoder and decoder are this machine's way of rendering (they don't change what's tracked).
    analysis_proxy_encoder: str = "libx264"
    # Where originals are decoded: "auto" (the GPU through Vulkan when it
    # works), "cpu", or an ffmpeg hwaccel such as "cuda". Same pictures either way.
    analysis_proxy_decoder: str = "auto"
    # Renders that decode on the GPU at once; the rest decode on the CPU
    # meanwhile. Each takes a few hundred MB of video memory beside the models.
    gpu_decodes: int = 1
    # Analysis proxies rendered at once; 0: one per six cores, at most three.
    render_concurrency: int = 0
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
