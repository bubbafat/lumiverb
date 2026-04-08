#!/usr/bin/env python3
"""Convert InsightFace ArcFace (buffalo_l) recognition model to CoreML.

Produces ArcFace.mlmodelc that the macOS app loads for on-device face
embeddings. The converted model must produce vectors with cosine similarity
> 0.999 vs the ONNX original.

Usage:
    cd scripts/convert-models
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python "coremltools==8.3" "onnxruntime==1.21.1" "insightface==0.7.3" "onnx==1.17.0" numpy Pillow
    .venv/bin/python convert_arcface.py

Output:
    ~/.lumiverb/models/ArcFace.mlmodelc
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import coremltools as ct
import numpy as np
import torch

OUTPUT_DIR = Path.home() / ".lumiverb" / "models"
INPUT_SIZE = 112  # ArcFace expects 112x112


def find_recognition_model() -> Path:
    """Find the ArcFace recognition ONNX model from insightface's model cache."""
    model_dir = Path.home() / ".insightface" / "models" / "buffalo_l"
    candidates = [
        model_dir / "w600k_r50.onnx",
    ]

    for p in candidates:
        if p.exists():
            return p

    # If not cached, trigger download by importing insightface
    print("ArcFace model not cached. Downloading buffalo_l via insightface...")
    from insightface.app import FaceAnalysis

    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=(640, 640))

    for p in candidates:
        if p.exists():
            return p

    raise FileNotFoundError(
        f"Could not find ArcFace recognition model. Checked: {candidates}"
    )


def convert(output_dir: Path = OUTPUT_DIR) -> Path:
    onnx_path = find_recognition_model()
    print(f"Found ONNX model: {onnx_path} ({onnx_path.stat().st_size / 1024 / 1024:.1f} MB)")

    # Load ONNX as PyTorch model via onnx2torch
    from onnx2torch import convert as onnx2torch_convert

    print("Converting ONNX → PyTorch...")
    torch_model = onnx2torch_convert(str(onnx_path))
    torch_model.eval()

    # Trace the model
    print("Tracing model...")
    dummy = torch.randn(1, 3, INPUT_SIZE, INPUT_SIZE)
    traced = torch.jit.trace(torch_model, dummy)

    # Convert to CoreML
    print("Converting PyTorch → CoreML...")
    mlmodel = ct.convert(
        traced,
        inputs=[
            ct.ImageType(
                name="input",
                shape=(1, 3, INPUT_SIZE, INPUT_SIZE),
                scale=1.0 / 127.5,
                bias=[-1.0, -1.0, -1.0],  # normalize to [-1, 1]
                color_layout=ct.colorlayout.RGB,
            )
        ],
        outputs=[ct.TensorType(name="output")],
        minimum_deployment_target=ct.target.macOS14,
    )

    # Save .mlpackage
    output_dir.mkdir(parents=True, exist_ok=True)
    mlpackage_path = output_dir / "ArcFace.mlpackage"
    mlmodel.save(str(mlpackage_path))
    print(f"Saved CoreML package to {mlpackage_path}")

    # Compile to .mlmodelc
    compiled_path = output_dir / "ArcFace.mlmodelc"
    print(f"Compiling to {compiled_path}...")
    result = subprocess.run(
        ["xcrun", "coremlcompiler", "compile", str(mlpackage_path), str(output_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  xcrun compile failed: {result.stderr}", file=sys.stderr)
        print("  The .mlpackage is still usable — the app will compile on first load.")
    else:
        print(f"  Compiled model at {compiled_path}")

    return mlpackage_path


FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "clients"
    / "lumiverb-app"
    / "Sources"
    / "LumiverbKit"
    / "Tests"
    / "LumiverbKitTests"
    / "Fixtures"
)

MIN_COS_SIM_REAL_FACES = 0.999


def _collect_aligned_face_crops(max_crops: int = 20) -> list[np.ndarray]:
    """Run InsightFace's detection + alignment on the LumiverbKit face fixtures
    and return a list of aligned 112×112 RGB face crops as uint8 arrays.

    Using real faces through the full InsightFace alignment pipeline gives us
    realistic inputs for validation — inputs that look like what the model
    actually sees in production, not uniform noise.
    """
    from insightface.app import FaceAnalysis
    from PIL import Image
    from insightface.utils import face_align

    fixtures = [
        FIXTURE_DIR / "face_single.jpg",
        FIXTURE_DIR / "face_group.jpg",
        FIXTURE_DIR / "face_crowd.jpg",
    ]

    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=(640, 640))

    crops: list[np.ndarray] = []
    for path in fixtures:
        if not path.exists():
            print(f"  (missing fixture: {path})", file=sys.stderr)
            continue
        pil_img = Image.open(path).convert("RGB")
        img_bgr = np.array(pil_img)[:, :, ::-1].copy()
        faces = app.get(img_bgr)
        if not faces:
            print(f"  (no faces detected in {path.name})", file=sys.stderr)
            continue
        for face in faces:
            if face.kps is None:
                continue
            aligned_bgr = face_align.norm_crop(img_bgr, landmark=face.kps)
            aligned_rgb = aligned_bgr[:, :, ::-1].copy()  # BGR → RGB
            crops.append(aligned_rgb)
            if len(crops) >= max_crops:
                return crops

    return crops


def validate(mlpackage_path: Path) -> None:
    """Validate CoreML output matches ONNX on real face images.

    Runs real photos through InsightFace's detection + alignment pipeline to
    produce realistic 112×112 aligned face crops, then compares the embedding
    InsightFace's own `ArcFaceONNX.get_feat()` produces to the embedding the
    converted CoreML model produces for the *same* aligned crop.

    InsightFace internally uses `cv2.dnn.blobFromImages(..., swapRB=True)`
    with mean 127.5 / std 127.5, so its pipeline consumes a BGR `numpy` array
    and feeds RGB to the model. The CoreML `ImageType` is configured with
    `color_layout=RGB` and the same scale/bias, so passing an RGB `PIL.Image`
    produces the equivalent model input — both pipelines should yield
    numerically near-identical 512-d vectors.

    Requires min cosine similarity ≥ 0.999 across all crops. Any drift
    indicates the ONNX → PyTorch → CoreML conversion has introduced
    numerical error that will degrade face-clustering quality.
    """
    from insightface.model_zoo.arcface_onnx import ArcFaceONNX
    from PIL import Image

    print("\nValidating with real aligned face crops from LumiverbKit fixtures...")

    crops_rgb = _collect_aligned_face_crops()
    if not crops_rgb:
        print(
            "  FAIL: No aligned face crops available for validation. "
            "Check that fixture images exist and InsightFace can detect them.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"  Collected {len(crops_rgb)} aligned face crops.")

    # Use InsightFace's own ArcFaceONNX wrapper for the ONNX side so the
    # preprocessing (mean/std, channel swap) exactly matches what the server
    # uses today. This eliminates any chance of a spurious validation failure
    # caused by preprocessing divergence in the test script itself.
    onnx_path = find_recognition_model()
    rec = ArcFaceONNX(str(onnx_path))
    rec.prepare(ctx_id=0)

    mlmodel = ct.models.MLModel(str(mlpackage_path))

    similarities = []
    for i, crop_rgb in enumerate(crops_rgb):
        # ONNX path: InsightFace's get_feat consumes BGR numpy arrays.
        crop_bgr = crop_rgb[:, :, ::-1].copy()
        onnx_emb = rec.get_feat(crop_bgr).flatten()
        onnx_emb = onnx_emb / np.linalg.norm(onnx_emb)

        # CoreML path: the model declares color_layout=RGB, so we pass the
        # RGB PIL image directly. CoreML applies scale/bias from the ImageType
        # config, matching InsightFace's (x − 127.5) / 127.5 internally.
        pil_img = Image.fromarray(crop_rgb)
        coreml_out = mlmodel.predict({"input": pil_img})
        ct_emb = np.array(coreml_out["output"]).flatten()
        ct_emb = ct_emb / np.linalg.norm(ct_emb)

        cos_sim = float(np.dot(onnx_emb, ct_emb))
        similarities.append(cos_sim)
        print(f"  Crop {i + 1}: cosine similarity = {cos_sim:.6f}")

    avg = float(np.mean(similarities))
    min_sim = float(np.min(similarities))
    print(f"\n  Average: {avg:.6f}, Min: {min_sim:.6f}")

    if min_sim < MIN_COS_SIM_REAL_FACES:
        print(
            f"  FAIL: Minimum similarity {min_sim:.6f} below "
            f"{MIN_COS_SIM_REAL_FACES} threshold — CoreML conversion "
            "has drifted and will degrade face-clustering quality.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        print("  PASS: All aligned-face embeddings match within threshold.")


def main():
    parser = argparse.ArgumentParser(description="Convert InsightFace ArcFace to CoreML")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    mlpackage_path = convert(args.output_dir)

    if not args.skip_validation:
        validate(mlpackage_path)

    print(f"\nDone! The macOS app will pick up the model from:")
    print(f"  {args.output_dir}/ArcFace.mlmodelc")


if __name__ == "__main__":
    main()
