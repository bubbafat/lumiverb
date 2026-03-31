"""Local CLI config: API URL and key, stored in ~/.lumiverb/config.json."""

from pathlib import Path

from pydantic import BaseModel


class CLIConfig(BaseModel):
    """CLI configuration stored in ~/.lumiverb/config.json."""

    api_url: str = "http://localhost:8000"
    api_key: str = ""
    admin_key: str = ""
    vision_api_url: str = ""
    vision_api_key: str = ""
    vision_model_id: str = ""
    face_batch_size: int = 50


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
