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
    api_listen_host: str = "127.0.0.1"  # uvicorn bind address (set via env/systemd)
    api_port: int = 8000
    api_secret_key: str = ""
    admin_key: str = ""

    # Metadata
    exiftool_path: str = "exiftool"
    sharpness_max_variance: float = 1000.0

    # Auth (JWT + password reset)
    jwt_secret: str = ""  # Required for JWT auth; generate with: openssl rand -hex 32
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    app_host: str = ""  # Public URL, e.g. https://app.example.com

    # App
    app_env: str = "development"
    log_level: str = "DEBUG"

    # Embedding
    clip_model_name: str = "ViT-B-32"
    clip_pretrained: str = "openai"

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
