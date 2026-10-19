"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Control plane
    control_plane_database_url: str
    tenant_database_url_template: str  # contains {tenant_id}

    # Storage: the Store's provider (src/server/storage/store.py); an install naming one there isn't refuses to start
    storage_provider: str = "local"
    data_dir: str = "./data"
    # Each account's playback cuts (src/server/storage/cache.py playback_cuts): at most this many GB.
    lumiverb_playback_cache_gb: float = 2.0

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
    # Forgot password on this machine itself gives the reset link at once: the
    # person at it owns the install (a Mac's: init --reset-on-this-machine).
    # Never on a server, where "on this machine" isn't "the operator".
    lumiverb_reset_on_this_machine: bool = False

    # App
    app_env: str = "development"
    log_level: str = "DEBUG"

    # Embedding
    clip_model_name: str = "ViT-B-32"
    clip_pretrained: str = "openai"

    # A Mac's own nginx, which lumiverb-server run starts: where it listens is
    # Settings › Network's (src/server/lifecycle/network.py)
    nginx_config: str = ""
    web_port: int = 0
    # Where other devices reach it over HTTPS, by default (lifecycle/network.py:
    # Settings › Network changes it); 0: 6465.
    web_tls_port: int = 0

    @field_validator("storage_provider")
    @classmethod
    def _a_provider_there_is(cls, value: str) -> str:
        from src.server.storage.store import PROVIDERS

        if value not in PROVIDERS:
            raise ValueError(f"STORAGE_PROVIDER={value!r} isn't a storage provider: the providers are "
                             f"{', '.join(PROVIDERS)} (ADR-020 decision 8)")
        return value

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
