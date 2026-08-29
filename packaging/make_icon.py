#!/usr/bin/env python3
"""Generate AppIcon.icns for nd2wsi-viewer (macOS Big Sur icon grid).

The mark is the product itself: a resolution pyramid — three stacked levels
in the system-blue ramp, each with a specular top edge, inside the standard
824x824 squircle (r185) on a 1024 canvas. Needs Pillow + macOS sips/iconutil.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "AppIcon.icns"


def squircle_background() -> Image.Image:
    img = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    # vertical gradient inside the squircle
    grad = Image.new("RGBA", (1024, 1024))
    top, bottom = (44, 44, 46), (13, 13, 15)
    for y in range(1024):
        t = y / 1023
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        ImageDraw.Draw(grad).line([(0, y), (1024, y)], fill=c + (255,))
    mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(mask).rounded_rectangle((100, 100, 924, 924), radius=185, fill=255)
    img.paste(grad, (0, 0), mask)
    # rim light
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((100, 100, 924, 924), radius=185,
                        outline=(255, 255, 255, 28), width=3)
    return img


def draw_pyramid(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    cx = 512
    levels = [  # (width, color) top -> bottom: the blue ramp
        (260, (124, 192, 255)),
        (420, (63, 160, 255)),
        (580, (10, 132, 255)),
    ]
    h, gap, r = 118, 40, 34
    total = 3 * h + 2 * gap
    y = 512 - total // 2 + 12
    for w, col in levels:
        box = (cx - w // 2, y, cx + w // 2, y + h)
        # soft drop
        d.rounded_rectangle((box[0] + 6, box[1] + 10, box[2] + 6, box[3] + 10),
                            radius=r, fill=(0, 0, 0, 70))
        d.rounded_rectangle(box, radius=r, fill=col + (255,))
        # specular top edge + counter-light bottom
        d.rounded_rectangle((box[0] + 8, box[1] + 4, box[2] - 8, box[1] + 12),
                            radius=6, fill=(255, 255, 255, 90))
        d.rounded_rectangle((box[0] + 10, box[3] - 8, box[2] - 10, box[3] - 4),
                            radius=4, fill=(255, 255, 255, 30))
        y += h + gap


def main() -> int:
    art = squircle_background()
    draw_pyramid(art)
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "icon_1024.png"
        art.save(png)
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        for s in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ""), (2, "@2x")):
                px = s * scale
                subprocess.run(
                    ["sips", "-z", str(px), str(px), str(png), "--out",
                     str(iconset / f"icon_{s}x{s}{suffix}.png")],
                    check=True, capture_output=True,
                )
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)],
                       check=True)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
