"""InsightFace provider: face detection + ArcFace embedding in one pass."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MODEL_ID = "insightface"
MODEL_VERSION = "buffalo_l"

# ── Quality-gate thresholds ──────────────────────────────────────────────
MIN_DETECTION_CONFIDENCE = 0.5   # InsightFace det_score
MIN_BBOX_AREA_FRACTION = 0.003  # 0.3% of image area
MIN_FACE_PIXELS = 40            # minimum width in pixels
MIN_RELATIVE_SIZE = 0.15        # must be ≥ 15% area of the largest face
MIN_LAPLACIAN_VARIANCE = 15.0   # sharpness floor (tune from logged values)


@dataclass
class FaceDetection:
    """Single detected face with bounding box, confidence, and embedding."""

    bounding_box: dict[str, float]  # {x, y, w, h} as fractions 0.0-1.0
    detection_confidence: float
    embedding: list[float]  # 512-dim, L2-normalized
    sharpness: float = 0.0  # Laplacian variance of face crop


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

    def _load(self):
        if self._app is None:
            with self._lock:
                if self._app is None:
                    import onnxruntime as ort
                    from insightface.app import FaceAnalysis

                    available = set(ort.get_available_providers())

                    # Build provider list with options for GPU/ANE acceleration
                    providers: list = []
                    if "CUDAExecutionProvider" in available:
                        providers.append("CUDAExecutionProvider")
                    if "CoreMLExecutionProvider" in available:
                        # MLComputeUnits=ALL enables GPU + ANE, not just CPU
                        providers.append(("CoreMLExecutionProvider", {"MLComputeUnits": "ALL"}))
                    providers.append("CPUExecutionProvider")

                    app = FaceAnalysis(
                        name=MODEL_VERSION,
                        providers=providers,
                    )
                    app.prepare(ctx_id=0, det_size=(640, 640))
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

        if not faces:
            del img_bgr
            return []

        # ── First pass: build candidates, apply per-face gates ───────────
        import cv2

        candidates: list[tuple[FaceDetection, float]] = []  # (detection, bbox_area)

        for face in faces:
            bbox = face.bbox  # [x1, y1, x2, y2] in pixels
            x1, y1, x2, y2 = bbox

            confidence = float(face.det_score)
            if confidence < MIN_DETECTION_CONFIDENCE:
                continue

            face_px_w = float(x2 - x1)
            if face_px_w < MIN_FACE_PIXELS:
                continue

            # Normalize to fractions of image dimensions, clamp to [0, 1]
            bx = max(0.0, min(1.0, float(x1) / w))
            by = max(0.0, min(1.0, float(y1) / h))
            bw = max(0.0, min(1.0, float(x2 - x1) / w))
            bh = max(0.0, min(1.0, float(y2 - y1) / h))

            bbox_area = bw * bh
            if bbox_area < MIN_BBOX_AREA_FRACTION:
                continue

            # ArcFace embedding — already L2-normalized by InsightFace
            embedding = face.normed_embedding
            if embedding is None:
                continue

            # Sharpness: Laplacian variance of the face crop (grayscale)
            px1 = max(0, int(x1))
            py1 = max(0, int(y1))
            px2 = min(w, int(x2))
            py2 = min(h, int(y2))
            if px2 > px1 and py2 > py1:
                face_crop = img_bgr[py1:py2, px1:px2]
                gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY)
                sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            else:
                sharpness = 0.0

            logger.debug(
                "face candidate: conf=%.3f area=%.4f px_w=%.0f sharpness=%.1f",
                confidence, bbox_area, face_px_w, sharpness,
            )

            if sharpness < MIN_LAPLACIAN_VARIANCE:
                continue

            vec = np.asarray(embedding, dtype=float).tolist()
            candidates.append((
                FaceDetection(
                    bounding_box={"x": bx, "y": by, "w": bw, "h": bh},
                    detection_confidence=confidence,
                    embedding=vec,
                    sharpness=sharpness,
                ),
                bbox_area,
            ))

        del faces, img_bgr

        if not candidates:
            return []

        # ── Second pass: relative size gate ──────────────────────────────
        max_area = max(area for _, area in candidates)
        results: list[FaceDetection] = []
        for det, area in candidates:
            if area < max_area * MIN_RELATIVE_SIZE:
                logger.debug(
                    "dropping face (relative size): area=%.4f max=%.4f ratio=%.3f",
                    area, max_area, area / max_area,
                )
                continue
            results.append(det)

        return results
