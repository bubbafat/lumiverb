"""InsightFace provider: face detection + ArcFace embedding in one pass."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MODEL_ID = "insightface"
MODEL_VERSION = "buffalo_l"


@dataclass
class FaceDetection:
    """Single detected face with bounding box, confidence, and embedding."""

    bounding_box: dict[str, float]  # {x, y, w, h} as fractions 0.0-1.0
    detection_confidence: float
    embedding: list[float]  # 512-dim, L2-normalized


class InsightFaceProvider:
    """
    Detects faces and generates 512-dim ArcFace embeddings using InsightFace buffalo_l.

    Lazy-loads model on first call (thread-safe). Runs on CPU via onnxruntime.
    """

    def __init__(self) -> None:
        self._app = None
        self._lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def model_version(self) -> str:
        return MODEL_VERSION

    def _load(self):
        if self._app is None:
            with self._lock:
                if self._app is None:
                    from insightface.app import FaceAnalysis

                    app = FaceAnalysis(
                        name=MODEL_VERSION,
                        providers=["CPUExecutionProvider"],
                    )
                    app.prepare(ctx_id=-1, det_size=(640, 640))
                    self._app = app
                    logger.info("Loaded InsightFace model %s (CPU)", MODEL_VERSION)
        return self._app

    def detect_faces(self, pil_image: "PIL.Image.Image") -> list[FaceDetection]:
        """Detect faces and generate ArcFace embeddings in one pass.

        Args:
            pil_image: RGB PIL Image.

        Returns:
            List of FaceDetection with bounding boxes as fractions of image
            dimensions and L2-normalized 512-dim embeddings.
        """
        import numpy as np

        app = self._load()

        # InsightFace expects BGR numpy array
        img_array = np.array(pil_image)
        if img_array.ndim == 2:
            img_array = np.stack([img_array] * 3, axis=-1)
        # RGB to BGR
        img_bgr = img_array[:, :, ::-1].copy()

        faces = app.get(img_bgr)
        if not faces:
            return []

        h, w = img_bgr.shape[:2]
        results: list[FaceDetection] = []

        for face in faces:
            bbox = face.bbox  # [x1, y1, x2, y2] in pixels
            x1, y1, x2, y2 = bbox

            # Normalize to fractions of image dimensions, clamp to [0, 1]
            bx = max(0.0, float(x1) / w)
            by = max(0.0, float(y1) / h)
            bw = min(1.0, float(x2 - x1) / w)
            bh = min(1.0, float(y2 - y1) / h)

            confidence = float(face.det_score)

            # ArcFace embedding — already L2-normalized by InsightFace
            embedding = face.normed_embedding
            if embedding is None:
                continue
            vec = np.asarray(embedding, dtype=float).tolist()

            results.append(FaceDetection(
                bounding_box={"x": bx, "y": by, "w": bw, "h": bh},
                detection_confidence=confidence,
                embedding=vec,
            ))

        return results
