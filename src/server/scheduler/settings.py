"""The scheduler's own settings: this machine's way of processing
(src/processing/machine.py) and where the API is, from the environment
(on Linux, /etc/lumiverb/env through its systemd unit; in development
.env and .env.local). Never the CLI's config.

    LUMIVERB_ROOT_MAP              library roots as the server stores them -> here, JSON:
                                   '{"/Volumes/media-01": "/mnt/media-01"}'; a library
                                   whose root isn't under it isn't read. A Mac with the
                                   files: '{"/": "/"}' (init --local-roots writes it)
    LUMIVERB_REFUSED_ROOTS         folders never read besides DATA_DIR, JSON: '["/opt/homebrew/etc/lumiverb"]'
                                   (lumiverb-server run sets the install's settings folder)
    LUMIVERB_ANALYSIS_PROXY_ENCODER  libx264 | h264_nvenc: H.264 only, which every browser plays
                                   (ADR-018 decision 11; the web player streams the proxy)
    LUMIVERB_ANALYSIS_PROXY_DECODER  auto (Vulkan, else a Mac's VideoToolbox) | cpu | an ffmpeg hwaccel (cuda)
    LUMIVERB_GPU_DECODES           renders decoding on the GPU at once (1; a Mac's VideoToolbox takes every render; 0: none);
                                   AI machines sharing the GPU give up requests only for those (Machine.gpu_decodes_here)
    LUMIVERB_RENDER_CONCURRENCY    analysis proxies rendered at once (0: one per six cores, at most three)
    LUMIVERB_ANALYSIS_CACHE_GB     the analysis proxies kept here (50): only those the API doesn't
                                   keep on this disk (they're read where it stores them)
    LUMIVERB_PREVIEW_CACHE_GB      the previews kept here (2), the same way: past it the least
                                   recently used go, made again or downloaded when next needed
    LUMIVERB_FACE_BATCHES_PER_PROCESS  face batches before a fresh detection process (20)
    LUMIVERB_FULL_SCAN_EVERY_MIN   each library scanned in full this often, in minutes (15): where nothing
                                   reports changes (a Mac running the server), how new files are found;
                                   a big library on a network share can raise it
    LUMIVERB_API_URL               the API (else the API's API_PORT on API_LISTEN_HOST)

Caches go under XDG_CACHE_HOME (the unit sets it to the data disk). A
setting the scheduler can't run with stops it at its start, saying which
(read_settings).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings import SettingsError as UnparsedSetting

from src.processing.machine import FULL_SCAN_EVERY_MIN, Machine


class SchedulerSettings(BaseSettings):
    root_map: dict[str, str] = Field(default_factory=dict)
    refused_roots: list[str] = Field(default_factory=list)
    analysis_proxy_encoder: Literal["libx264", "h264_nvenc"] = "libx264"
    analysis_proxy_decoder: str = "auto"
    gpu_decodes: int = Field(default=1, ge=0)
    render_concurrency: int = Field(default=0, ge=0)
    analysis_cache_gb: float = Field(default=50.0, gt=0)
    preview_cache_gb: float = Field(default=2.0, gt=0)
    face_batches_per_process: int = Field(default=20, ge=1)
    # Minutes; never infinite ("inf" would mean no full scan after the first).
    full_scan_every_min: float = Field(default=FULL_SCAN_EVERY_MIN, gt=0, allow_inf_nan=False)
    api_url: str = ""

    model_config = SettingsConfigDict(env_prefix="LUMIVERB_", env_file=(".env", ".env.local"),
                                      env_file_encoding="utf-8", extra="ignore")

    def machine(self) -> Machine:
        """Every account's libraries are read here: only through the root map,
        never the server's data (DATA_DIR), its previews, or another refused
        folder."""
        from src.server.config import get_settings

        return Machine(root_map=dict(self.root_map), mapped_roots_only=True,
                       refused_roots=(get_settings().data_dir, *self.refused_roots), data_dir=get_settings().data_dir,
                       analysis_proxy_encoder=self.analysis_proxy_encoder,
                       analysis_proxy_decoder=self.analysis_proxy_decoder, gpu_decodes=self.gpu_decodes,
                       render_concurrency=self.render_concurrency, analysis_cache_gb=self.analysis_cache_gb,
                       preview_cache_gb=self.preview_cache_gb,
                       face_batches_per_process=self.face_batches_per_process,
                       full_scan_every_min=self.full_scan_every_min)


class SettingError(ValueError):
    """A setting in the scheduler's environment it can't run with."""


def read_settings() -> SchedulerSettings:
    """The scheduler's settings from its environment. SettingError names
    each one it can't run with: the variable, what's wrong and what it says."""
    try:
        return SchedulerSettings()
    except ValidationError as exc:
        raise SettingError("; ".join(
            f"LUMIVERB_{str(e['loc'][0]).upper() if e['loc'] else ''}: {e['msg']} (it says {e['input']!r})"
            for e in exc.errors())) from None
    except UnparsedSetting as exc:  # names the field: "root_map" is LUMIVERB_ROOT_MAP
        raise SettingError(f"{exc}: it isn't JSON") from None


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
