"""Video preview worker: generate a short MP4 preview clip for video assets."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from src.storage.local import LocalStorage
from src.workers.base import BaseWorker

logger = logging.getLogger(__name__)

PREVIEW_DURATION_SEC = 10
PREVIEW_MAX_HEIGHT = 720


class VideoPreviewWorker(BaseWorker):
    job_type = "video-preview"

    def __init__(
        self,
        client: object,
        storage: LocalStorage,
        tenant_id: str,
        concurrency: int = 1,
        once: bool = False,
        library_id: str | None = None,
    ) -> None:
        super().__init__(client, concurrency=concurrency, once=once, library_id=library_id)
        self._storage = storage
        self._tenant_id = tenant_id

    def process(self, job: dict) -> dict:
        asset_id = job["asset_id"]
        rel_path = job["rel_path"]
        root_path = job["root_path"]
        library_id = job["library_id"]

        root = Path(root_path).resolve()
        source_path = (root / rel_path).resolve()
        if not source_path.is_relative_to(root):
            raise ValueError(f"rel_path escapes library root: {rel_path!r}")
        if not source_path.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        preview_key = self._storage.video_preview_key(
            self._tenant_id,
            library_id,
            asset_id,
            rel_path,
        )
        preview_path = self._storage.abs_path(preview_key)
        preview_path.parent.mkdir(parents=True, exist_ok=True)

        # Build ffmpeg command to extract first PREVIEW_DURATION_SEC seconds,
        # re-encoding to H.264/AAC in MP4 container with a modest resolution.
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0",
            "-i",
            str(source_path),
            "-t",
            str(PREVIEW_DURATION_SEC),
            "-vf",
            f"scale=-2:'min({PREVIEW_MAX_HEIGHT},ih)',format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(preview_path),
        ]

        logger.info("Generating video preview for asset_id=%s via ffmpeg", asset_id)
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            def _decode(raw: bytes | str | None) -> str:
                if raw is None:
                    return ""
                if isinstance(raw, bytes):
                    return raw.decode("utf-8", errors="replace")
                return raw

            first_stderr = _decode(getattr(exc, "stderr", None)).strip()

            # Some camera/iPhone MOVs can fail due to the audio track; retry without audio.
            no_audio_cmd: list[str] = []
            skip_next = False
            for tok in cmd:
                if skip_next:
                    skip_next = False
                    continue
                if tok in {"-c:a", "-ac", "-b:a"}:
                    skip_next = True
                    continue
                if tok == "aac":
                    # value for -c:a
                    continue
                no_audio_cmd.append(tok)

            # Ensure audio disabled (in case an audio stream exists).
            if "-an" not in no_audio_cmd:
                try:
                    idx = no_audio_cmd.index("-movflags")
                except ValueError:
                    idx = len(no_audio_cmd) - 1
                no_audio_cmd.insert(idx, "-an")

            try:
                subprocess.run(no_audio_cmd, check=True, capture_output=True)
                logger.warning(
                    "ffmpeg preview succeeded only after no-audio retry for asset_id=%s source=%s",
                    asset_id,
                    source_path,
                )
            except subprocess.CalledProcessError as exc2:
                second_stderr = _decode(getattr(exc2, "stderr", None)).strip()
                raise RuntimeError(
                    "ffmpeg failed for "
                    f"{source_path} (exit {exc.returncode}): {first_stderr}\n"
                    f"ffmpeg no-audio retry failed (exit {exc2.returncode}): {second_stderr}"
                ) from exc2

        return {"video_preview_key": preview_key}

