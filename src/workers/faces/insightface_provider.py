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
        # FaceAnalysis wraps ONNX Runtime; InferenceSession is not safe for concurrent
        # Run() from multiple threads — shared use from ingest/repair thread pools caused
        # unbounded RSS growth (~tens of MB per image). Serialize inference.
        self._infer_lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def model_version(self) -> str:
        return MODEL_VERSION

    # Preferred ONNX execution providers in priority order.
    # ONNX Runtime tries each and silently skips unavailable ones.
    _PROVIDERS = [
        "CUDAExecutionProvider",      # NVIDIA GPU
        "CoreMLExecutionProvider",    # Apple Silicon Neural Engine / GPU
        "CPUExecutionProvider",       # Always available fallback
    ]

    def _load(self):
        if self._app is None:
            with self._lock:
                if self._app is None:
                    import onnxruntime as ort
                    from insightface.app import FaceAnalysis

                    available = set(ort.get_available_providers())
                    providers = [p for p in self._PROVIDERS if p in available]

                    app = FaceAnalysis(
                        name=MODEL_VERSION,
                        providers=providers,
                    )
                    app.prepare(ctx_id=-1, det_size=(640, 640))
                    self._app = app
                    active = providers[0] if providers else "unknown"
                    logger.info("Loaded InsightFace model %s (%s)", MODEL_VERSION, active)
        return self._app

    def ensure_loaded(self) -> None:
        """Force model loading now (fail fast). Thread-safe."""
        self._load()

    # Max long edge for face detection input. InsightFace resizes to 640x640
    # internally for detection anyway — larger inputs just waste memory.
    # Bounding boxes are returned as fractions, so resizing is transparent.
    _MAX_DETECT_EDGE = 1280

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

        # Downscale if larger than _MAX_DETECT_EDGE to reduce memory.
        # Bounding boxes are normalized to fractions, so this is transparent.
        w_orig, h_orig = pil_image.size
        long_edge = max(w_orig, h_orig)
        if long_edge > self._MAX_DETECT_EDGE:
            scale = self._MAX_DETECT_EDGE / long_edge
            new_w = int(w_orig * scale)
            new_h = int(h_orig * scale)
            pil_image = pil_image.resize((new_w, new_h))

        # Convert PIL RGB to BGR numpy array for InsightFace
        img_array = np.array(pil_image)
        if img_array.ndim == 2:
            img_array = np.stack([img_array] * 3, axis=-1)
        img_bgr = img_array[:, :, ::-1].copy()
        del img_array  # free RGB copy

        with self._infer_lock:
            faces = app.get(img_bgr)
        h, w = img_bgr.shape[:2]
        del img_bgr  # free BGR copy

        if not faces:
            return []

        results: list[FaceDetection] = []

        for face in faces:
            bbox = face.bbox  # [x1, y1, x2, y2] in pixels
            x1, y1, x2, y2 = bbox

            # Normalize to fractions of image dimensions, clamp to [0, 1]
            bx = max(0.0, min(1.0, float(x1) / w))
            by = max(0.0, min(1.0, float(y1) / h))
            bw = max(0.0, min(1.0, float(x2 - x1) / w))
            bh = max(0.0, min(1.0, float(y2 - y1) / h))

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

        del faces  # free InsightFace result objects (contain large numpy arrays)
        return results
