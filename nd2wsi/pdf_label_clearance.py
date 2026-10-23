"""Transparent clearances around PDF labels, applied while drawing vectors.

This is the generation-time equivalent of the supplied SG report postprocessor.
It clips paths instead of painting opaque label backgrounds or rewriting image
streams. Callers draw images before, and labels outside, ``clip_label_paths``.
All rectangles returned here use PDF bottom-left coordinates: x, y, width,
height. ``label_rect`` accepts the report layout's top-down text position.
"""
from __future__ import annotations

import math
from contextlib import contextmanager


def _finite(value, name):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def label_rect(text, x, top, size, font_name, page_height, gap=4.0):
    """Protect actual font ascent/descent plus ``gap`` points on each side.

    The matching ReportLab text baseline is ``page_height - top - size``.
    This deliberately uses font metrics, not the visible glyph bounding box,
    to match the reference script's embedded FontDescriptor calculation.
    """
    from reportlab.pdfbase import pdfmetrics

    x, top = _finite(x, "x"), _finite(top, "top")
    size = _finite(size, "font size")
    page_height, gap = _finite(page_height, "page height"), _finite(gap, "gap")
    if size <= 0 or page_height <= 0 or gap < 0:
        raise ValueError("Font size and page height must be positive; gap must be non-negative")
    ascent, descent = pdfmetrics.getAscentDescent(font_name, size)
    width = pdfmetrics.stringWidth(str(text), font_name, size)
    baseline = page_height - top - size
    return x - gap, baseline + descent - gap, width + 2 * gap, ascent - descent + 2 * gap


@contextmanager
def clip_label_paths(canvas, page_size, rectangles):
    """Clip vector paths to the page minus every padded label rectangle.

    Each page-minus-label path intersects the preceding clip. Combining all
    holes in one even-odd path would wrongly expose overlaps between labels.
    Images and text must be drawn outside this context; no pixels are erased.
    The canvas graphics state is restored even if vector drawing fails.
    """
    width, height = (_finite(value, "page size") for value in page_size)
    if width <= 0 or height <= 0:
        raise ValueError("Page dimensions must be positive")
    holes = []
    for rectangle in rectangles:
        x, y, w, h = (_finite(value, "label rectangle") for value in rectangle)
        if w < 0 or h < 0:
            raise ValueError("Label rectangle dimensions cannot be negative")
        if w and h:
            holes.append((x, y, w, h))
    canvas.saveState()
    canvas.addLiteral("/SGLabelClearance BMC")
    try:
        for x, y, w, h in holes:
            path = canvas.beginPath()
            path.rect(0, 0, width, height)
            path.rect(x, y, w, h)
            canvas.clipPath(path, stroke=0, fill=0, fillMode=0)
        yield
    finally:
        canvas.addLiteral("EMC")
        canvas.restoreState()
