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

    # Metadata
    exiftool_path: str = "exiftool"
    sharpness_max_variance: float = 1000.0

    # App
    app_env: str = "development"
    log_level: str = "DEBUG"

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
