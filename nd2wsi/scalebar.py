"""Calibrated, source-aligned scale bars for rendered region exports."""

from __future__ import annotations

import base64
import io
import json
import math
from dataclasses import asdict, dataclass
from html import escape

from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class ScaleBar:
    length_um: float
    pixel_size_x_um: float
    width: float
    x: float
    y: float
    thickness: float
    font_size: int
    label_y: float
    label: str


def layout(width: int, height: int, pixel_size_um, downsample: float) -> ScaleBar:
    """Use calibrated horizontal pixels at the actual exported pyramid level."""
    try:
        py, px = map(float, pixel_size_um)
        downsample = float(downsample)
    except (TypeError, ValueError):
        raise ValueError("Scale bar export requires valid pixel calibration") from None
    if not all(math.isfinite(v) and v > 0 for v in (py, px, downsample)):
        raise ValueError("Scale bar export requires valid pixel calibration")
    if width < 96 or height < 64:
        raise ValueError("Choose a larger export size for the scale bar (at least 96 × 64 px)")
    pixel_x = px * downsample
    target = width * pixel_x * 0.2
    if not math.isfinite(target) or target <= 0:
        raise ValueError("Scale bar export requires valid pixel calibration")
    power = 10 ** math.floor(math.log10(target))
    length = max(n * power for n in (1, 2, 5) if n * power <= target)
    unit = min(width / 796, height / 775)
    margin = max(4, 28 * unit)
    thickness = max(1, 6 * unit)
    y = height - max(4, 30 * unit) - thickness
    bar_width = length / pixel_x
    label = f"{length / 1000:g} mm" if length >= 1000 else f"{length:g} µm"
    return ScaleBar(length, pixel_x, bar_width, width - margin - bar_width,
                    y, thickness, max(8, round(24 * unit)), y - max(3, 11 * unit), label)


def svg(png: bytes, width: int, height: int, bar: ScaleBar, *, roi: dict) -> bytes:
    """Embed the unchanged PNG; keep the bar and its label separately editable."""
    metadata = escape(json.dumps({"scale_bar": asdict(bar), "roi": roi}, ensure_ascii=False))
    image = base64.b64encode(png).decode("ascii")
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <title>Microscopy region with calibrated scale bar</title>
  <metadata>{metadata}</metadata>
  <image id="microscopy-image" x="0" y="0" width="{width}" height="{height}"
         xlink:href="data:image/png;base64,{image}"/>
  <g id="scale-bar" fill="#ffffff" aria-label="{escape(bar.label)}">
    <rect id="scale-bar-line" x="{bar.x:.12g}" y="{bar.y:.12g}"
          width="{bar.width:.12g}" height="{bar.thickness:.12g}"/>
    <text id="scale-bar-label" x="{bar.x + bar.width / 2:.12g}" y="{bar.label_y:.12g}"
          text-anchor="middle" font-family="Arial, Helvetica, sans-serif"
          font-size="{bar.font_size}" font-weight="400">{escape(bar.label)}</text>
  </g>
</svg>
'''
    return xml.encode("utf-8")


def _font(size: int):
    for name in ("/System/Library/Fonts/Supplemental/Arial.ttf", "Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    # Pillow bundles a Unicode-capable scalable face when FreeType is enabled.
    return ImageFont.load_default(size=size)


def draw(image: Image.Image, bar: ScaleBar) -> Image.Image:
    """Antialias only the small annotation patch; never resample the source image."""
    width, height = image.size
    top = max(0, math.floor(bar.label_y - 2 * bar.font_size))
    left = max(0, math.floor(min(bar.x, bar.x + bar.width / 2 - len(bar.label) * bar.font_size)))
    size = (width - left, height - top)
    factor = 4
    mask = Image.new("L", (size[0] * factor, size[1] * factor), 0)
    painter = ImageDraw.Draw(mask)
    # Half-open bar geometry: avoid ImageDraw.rectangle's inclusive last pixel.
    painter.rectangle(((bar.x - left) * factor, (bar.y - top) * factor,
                       (bar.x + bar.width - left) * factor - 1,
                       (bar.y + bar.thickness - top) * factor - 1), fill=255)
    painter.text(((bar.x + bar.width / 2 - left) * factor, (bar.label_y - top) * factor),
                 bar.label, font=_font(bar.font_size * factor), fill=255, anchor="ms")
    mask = mask.resize(size, Image.Resampling.LANCZOS)
    image.paste((255, 255, 255), (left, top, width, height), mask)
    return image


def jpeg(image: Image.Image, bar: ScaleBar) -> bytes:
    out = io.BytesIO()
    draw(image, bar).save(out, format="JPEG", quality=98, subsampling=0)
    return out.getvalue()
