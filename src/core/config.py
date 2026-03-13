"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Control plane
    control_plane_database_url: str
    tenant_database_url_template: str  # contains {tenant_id}

    # Quickwit
    quickwit_url: str = "http://localhost:7280"
    quickwit_enabled: bool = True
    quickwit_fallback_to_postgres: bool = True

    # Storage
    storage_provider: str = "local"
    data_dir: str = "./data"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_secret_key: str = ""
    admin_key: str = ""

    # Moondream
    moondream_station_endpoint: str = "http://localhost:2020/v1"

    # Workers
    worker_idle_poll_seconds: float = 5.0
    worker_lease_minutes: int = 10
    search_sync_lease_minutes: int = 5

    # Metadata
    exiftool_path: str = "exiftool"
    sharpness_max_variance: float = 1000.0

    # App
    app_env: str = "development"
    log_level: str = "DEBUG"

    # Embedding
    clip_model_name: str = "ViT-B-32"
    clip_pretrained: str = "openai"
    embedding_moondream_weight: float = 0.3
    embedding_clip_weight: float = 0.7

    # Vision API (OpenAI-compatible endpoint for non-Moondream models)
    vision_api_url: str = "http://localhost:1234/v1"
    vision_api_key: str = ""  # Optional Bearer token for the vision API endpoint

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
