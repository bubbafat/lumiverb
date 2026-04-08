#!/usr/bin/env python3
"""Convert open-clip ViT-B/32 image encoder to CoreML.

Produces CLIPImageEncoder.mlmodelc that the macOS/iOS app loads for
on-device similarity embeddings. The converted model must produce vectors
with cosine similarity > 0.999 vs the PyTorch original.

Usage:
    cd scripts/convert-models
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python "torch==2.7.0" "coremltools==8.3" open-clip-torch Pillow numpy
    .venv/bin/python convert_clip.py

Output:
    ~/.lumiverb/models/CLIPImageEncoder.mlmodelc
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import coremltools as ct
import numpy as np
import open_clip
import torch
from PIL import Image

MODEL_NAME = "ViT-B-32"
PRETRAINED = "openai"
INPUT_SIZE = 224
OUTPUT_DIM = 512
OUTPUT_DIR = Path.home() / ".lumiverb" / "models"


class CLIPImageEncoderWrapper(torch.nn.Module):
    """Wraps the CLIP image encoder to output L2-normalized embeddings."""

    def __init__(self, model: torch.nn.Module):
        super().__init__()
        self.visual = model.visual

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        features = self.visual(x)
        features = features / features.norm(dim=-1, keepdim=True)
        return features


def convert(output_dir: Path = OUTPUT_DIR) -> tuple[Path, object]:
    print(f"Loading open-clip {MODEL_NAME} ({PRETRAINED})...")
    model, _, preprocess = open_clip.create_model_and_transforms(
        MODEL_NAME, pretrained=PRETRAINED
    )
    model.eval()

    wrapper = CLIPImageEncoderWrapper(model)
    wrapper.eval()

    # Trace the model (check_trace=False: attention internals produce
    # non-deterministic graph structure but identical output values)
    print("Tracing model...")
    dummy = torch.randn(1, 3, INPUT_SIZE, INPUT_SIZE)
    traced = torch.jit.trace(wrapper, dummy, check_trace=False)

    # Convert to CoreML
    print("Converting to CoreML...")
    mlmodel = ct.convert(
        traced,
        inputs=[
            ct.ImageType(
                name="input",
                shape=(1, 3, INPUT_SIZE, INPUT_SIZE),
                scale=1.0 / 255.0,
                color_layout=ct.colorlayout.RGB,
            )
        ],
        outputs=[ct.TensorType(name="output")],
        minimum_deployment_target=ct.target.macOS14,
    )

    # Save .mlpackage
    output_dir.mkdir(parents=True, exist_ok=True)
    mlpackage_path = output_dir / "CLIPImageEncoder.mlpackage"
    mlmodel.save(str(mlpackage_path))
    print(f"Saved CoreML package to {mlpackage_path}")

    # Compile to .mlmodelc
    compiled_path = output_dir / "CLIPImageEncoder.mlmodelc"
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

    return mlpackage_path, preprocess


def validate(mlpackage_path: Path, preprocess, n_images: int = 10) -> None:
    """Validate CoreML output matches PyTorch (cosine similarity > 0.999)."""
    print(f"\nValidating with {n_images} random images...")

    model, _, _ = open_clip.create_model_and_transforms(
        MODEL_NAME, pretrained=PRETRAINED
    )
    model.eval()

    mlmodel = ct.models.MLModel(str(mlpackage_path))

    similarities = []
    for i in range(n_images):
        img = Image.fromarray(
            np.random.randint(0, 255, (INPUT_SIZE, INPUT_SIZE, 3), dtype=np.uint8)
        )

        # PyTorch
        with torch.no_grad():
            tensor = preprocess(img).unsqueeze(0)
            pt_emb = model.encode_image(tensor)
            pt_emb = pt_emb / pt_emb.norm(dim=-1, keepdim=True)
            pt_emb = pt_emb.squeeze().numpy()

        # CoreML
        coreml_out = mlmodel.predict({"input": img})
        ct_emb = np.array(coreml_out["output"]).flatten()

        cos_sim = float(
            np.dot(pt_emb, ct_emb) / (np.linalg.norm(pt_emb) * np.linalg.norm(ct_emb))
        )
        similarities.append(cos_sim)
        print(f"  Image {i + 1}: cosine similarity = {cos_sim:.6f}")

    avg = np.mean(similarities)
    min_sim = np.min(similarities)
    print(f"\n  Average: {avg:.6f}, Min: {min_sim:.6f}")

    if min_sim < 0.999:
        print("  WARNING: Minimum similarity below 0.999 threshold!", file=sys.stderr)
        sys.exit(1)
    else:
        print("  PASS: All embeddings match within threshold.")


def main():
    parser = argparse.ArgumentParser(description="Convert CLIP ViT-B/32 to CoreML")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    mlpackage_path, preprocess = convert(args.output_dir)

    if not args.skip_validation:
        validate(mlpackage_path, preprocess)

    print(f"\nDone! The macOS app will pick up the model from:")
    print(f"  {args.output_dir}/CLIPImageEncoder.mlmodelc")


if __name__ == "__main__":
    main()
