#!/usr/bin/env python3
"""Generate synthetic ND2 fixtures with Laboratory Imaging's `limnd2` writer.

limnd2 (the ND2 SDK rewritten in Python by NIS-Elements' authors) lives on
their own package index:

    pip install --index-url https://pypi.laboratory-imaging.com/simple limnd2

Two fixtures:

* slide  -- one big single-frame RGB image, the same internal shape as a
            stitched "Scan Large Image" ND2 (one ImageDataSeq blob);
* fluor  -- a two-channel uint16 image with named/colored channels, for the
            multichannel compositing path.

The content is structured (gradients, grid, blobs) so that ROI exports can be
verified pixel-for-pixel against direct reads.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np


def slide_rgb(width: int, height: int) -> np.ndarray:
    yy = np.linspace(0, 1, height, dtype=np.float32)[:, None]
    xx = np.linspace(0, 1, width, dtype=np.float32)[None, :]
    r = (255 * xx * np.ones_like(yy)).astype(np.uint8)
    g = (255 * yy * np.ones_like(xx)).astype(np.uint8)
    b = ((np.sin(xx * 60 * np.pi) * np.sin(yy * 40 * np.pi) * 0.5 + 0.5) * 255).astype(
        np.uint8
    )
    img = np.stack([r, g, b], axis=-1)
    img[::1000, :, :] = 255
    img[:, ::1000, :] = 255
    return img


def fluor_2ch(width: int, height: int) -> np.ndarray:
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    ch0 = np.zeros((height, width), np.float32)
    ch1 = np.zeros((height, width), np.float32)
    for _ in range(160):
        cx, cy = rng.uniform(0, width), rng.uniform(0, height)
        s = rng.uniform(8, 40)
        blob = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * s * s)))
        (ch0 if rng.random() < 0.5 else ch1)[:] += blob * rng.uniform(4000, 20000)
    ch0 += rng.normal(400, 60, ch0.shape)
    ch1 += rng.normal(300, 50, ch1.shape)
    stack = np.stack([ch0, ch1], axis=-1)  # (Y, X, C): component-interleaved
    return np.clip(stack, 0, 65535).astype(np.uint16)


def write_nd2(path: Path, img: np.ndarray, channels: list[tuple[str, str]], px_um: float):
    import limnd2

    h, w = img.shape[:2]
    comps = 1 if img.ndim == 2 else img.shape[2]
    bits = img.dtype.itemsize * 8
    attrs = limnd2.ImageAttributes.create(
        width=w, height=h, component_count=comps, bits=bits, sequence_count=1
    )
    path.unlink(missing_ok=True)  # Nd2Writer appends to existing files
    with limnd2.Nd2Writer(str(path)) as f:
        f.imageAttributes = attrs
        f.setImage(0, img)
        mf = limnd2.MetadataFactory(
            objective_magnification=20.0, pixel_calibration=px_um
        )
        for name, color in channels:
            mf.addPlane(name=name, modality="Widefield Fluorescence", color=color)
        f.pictureMetadata = mf.createMetadata()
    print(f"wrote {path}  {w}x{h}  {comps} comp(s)  {img.dtype}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out_dir", nargs="?", default="testdata", type=Path)
    ap.add_argument("--slide-size", default="12800x9600", help="WxH for the RGB slide")
    ap.add_argument("--fluor-size", default="6400x4800", help="WxH for the 2ch image")
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    sw, sh = (int(v) for v in args.slide_size.lower().split("x"))
    fw, fh = (int(v) for v in args.fluor_size.lower().split("x"))

    write_nd2(
        args.out_dir / "synthetic_slide.nd2",
        slide_rgb(sw, sh),
        [("Brightfield", "white")],
        px_um=0.33,
    )
    write_nd2(
        args.out_dir / "synthetic_fluor.nd2",
        fluor_2ch(fw, fh),
        [("DAPI", "blue"), ("GFP", "green")],
        px_um=0.65,
    )


if __name__ == "__main__":
    main()
