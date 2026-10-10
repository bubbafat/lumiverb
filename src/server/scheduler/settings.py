"""The scheduler's own settings: this machine's way of processing
(src/processing/machine.py) and where the API is, from the environment
(on the brain, /etc/lumiverb/env through its systemd unit; in development
.env and .env.local). Never the CLI's config.

    LUMIVERB_ROOT_MAP              library roots as the server stores them -> here, JSON:
                                   '{"/Volumes/media-01": "/mnt/media-01"}'
    LUMIVERB_ANALYSIS_PROXY_ENCODER  libx264
    LUMIVERB_ANALYSIS_PROXY_DECODER  auto | cpu | an ffmpeg hwaccel (cuda)
    LUMIVERB_GPU_DECODES           renders decoding on the GPU at once (1)
    LUMIVERB_RENDER_CONCURRENCY    analysis proxies rendered at once (0: one per six cores, at most three)
    LUMIVERB_ANALYSIS_CACHE_GB     the analysis proxies kept here (50)
    LUMIVERB_FACE_BATCHES_PER_PROCESS  face batches before a fresh detection process (20)
    LUMIVERB_API_URL               the API (else the API's API_PORT on API_LISTEN_HOST)

Caches go under XDG_CACHE_HOME (the unit sets it to the data disk).
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.processing.machine import Machine


class SchedulerSettings(BaseSettings):
    root_map: dict[str, str] = Field(default_factory=dict)
    analysis_proxy_encoder: str = "libx264"
    analysis_proxy_decoder: str = "auto"
    gpu_decodes: int = Field(default=1, ge=0)
    render_concurrency: int = Field(default=0, ge=0)
    analysis_cache_gb: float = Field(default=50.0, gt=0)
    face_batches_per_process: int = Field(default=20, ge=1)
    api_url: str = ""

    model_config = SettingsConfigDict(env_prefix="LUMIVERB_", env_file=(".env", ".env.local"),
                                      env_file_encoding="utf-8", extra="ignore")

    def machine(self) -> Machine:
        return Machine(root_map=dict(self.root_map), analysis_proxy_encoder=self.analysis_proxy_encoder,
                       analysis_proxy_decoder=self.analysis_proxy_decoder, gpu_decodes=self.gpu_decodes,
                       render_concurrency=self.render_concurrency, analysis_cache_gb=self.analysis_cache_gb,
                       face_batches_per_process=self.face_batches_per_process)


def api_url(settings: SchedulerSettings | None = None) -> str:
    """Where the scheduler reaches the API: LUMIVERB_API_URL, else the API's
    own port on this machine (API_PORT and API_LISTEN_HOST, as it listens)."""
    from src.server.config import get_settings

    settings = settings or SchedulerSettings()
    if settings.api_url:
        return settings.api_url.rstrip("/")
    server = get_settings()
    host = server.api_listen_host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{server.api_port}"
