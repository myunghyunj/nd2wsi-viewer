"""SG_LAYOUT ROI navigation reports with vector labels and bounded image reads.

Geometry and label placement follow the supplied build_sg_reports.py. Label
clearance is applied while drawing, so it never paints over image pixels.
Only one ReportLab page is in memory; pdf_stream writes it to disk immediately.
"""
from __future__ import annotations

import io
import math
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.pdfgen import canvas

from . import render
from .pdf_label_clearance import clip_label_paths, label_rect
from .pdf_stream import StreamingPdf

PAGE_POINTS = (960.0, 5391397 / 12700)
PAGE_W, PAGE_H = PAGE_POINTS
RIGHT = (4304808 / 12700, 126065 / 12700, 7772400 / 12700, 5140991 / 12700)
DETAIL = (2004275 / 12700, 127465 / 12700, 2186042 / 12700, 2386596 / 12700)
WHOLE = (114792 / 12700, 2615619 / 12700, 4075224 / 12700, 2651438 / 12700)
BOX, DETAIL_SPAN, CONTEXT_W = 300, 1000, 3000
CONTEXT_H = round(CONTEXT_W * RIGHT[3] / RIGHT[2])
YELLOW, GREY, CYAN = (1, 1, 0), (.70, .73, .76), (0, 1, 1)
PDF_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _fonts():
    regular = ('/System/Library/Fonts/Supplemental/Arial.ttf', 'C:/Windows/Fonts/arial.ttf',
               '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf')
    bold = ('/System/Library/Fonts/Supplemental/Arial Bold.ttf', 'C:/Windows/Fonts/arialbd.ttf',
            '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf')
    result = []
    for name, paths, fallback in [('SGArial', regular, 'Helvetica'), ('SGArialBold', bold, 'Helvetica-Bold')]:
        for path in paths:
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                result.append(name)
                break
            except (OSError, ValueError, TTFError):
                pass
        else:
            result.append(fallback)
    for path in ('/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
                 '/System/Library/Fonts/Supplemental/AppleGothic.ttf',
                 'C:/Windows/Fonts/malgun.ttf'):
        try:
            pdfmetrics.registerFont(TTFont('SGUnicode', path))
            result.append('SGUnicode')
            break
        except (OSError, ValueError, TTFError):
            pass
    else:
        pdfmetrics.registerFont(UnicodeCIDFont('HYSMyeongJo-Medium'))
        result.append('HYSMyeongJo-Medium')
    return tuple(result)


def _text_font(text, bold=False):
    fonts = _fonts()
    return fonts[2] if any(ord(c) > 255 for c in text) else fonts[int(bold)]


def _source_size(plan):
    base = plan.attrs["nd2wsi"]["levels"][0]
    return int(base["width"]), int(base["height"])

def _fit_size(region, limit):
    scale = min(limit[0] / region[2], limit[1] / region[3])
    return max(1, round(region[2] * scale)), max(1, round(region[3] * scale))

def render_region(plan, region, limit):
    """Bilinear samples with bounded channel reads, even on sparse pyramids."""
    size = _fit_size(region, limit)
    output = Image.new("RGB", size)
    if not plan.channels:
        return output
    width, height = _source_size(plan)
    x, y, rw, rh = region
    levels = plan.levels or tuple(plan.attrs["nd2wsi"]["levels"])
    sufficient = [lv for lv in levels
                  if rw * lv["width"] / width >= size[0] and rh * lv["height"] / height >= size[1]]
    level = min(sufficient or [levels[0]], key=lambda lv: lv["width"] * lv["height"])
    arr = plan.root[str(level["path"])]
    _, lh, lw = arr.shape
    lx, ly = x * lw / width, y * lh / height
    sx, sy = rw * lw / width / size[0], rh * lh / height / size[1]
    # Every read spans <=512 source pixels per axis. At extreme reductions a
    # single output sample still needs only its adjacent 2x2 source pixels.
    tile = max(1, min(256, int(508 / max(sx, sy))))
    windows, colors = render.display_params(plan.attrs)
    windows, gammas = render.parse_windows(plan.window, windows)
    rgb = bool(plan.attrs["nd2wsi"].get("rgb"))
    try:
        for oy in range(0, size[1], tile):
            ey = min(size[1], oy + tile)
            ys = np.clip(ly + (np.arange(oy, ey) + .5) * sy - .5, 0, lh - 1)
            yi = np.floor(ys).astype(np.int64)
            y0, y1 = int(yi[0]), min(lh, int(yi[-1]) + 2)
            for ox in range(0, size[0], tile):
                ex = min(size[0], ox + tile)
                xs = np.clip(lx + (np.arange(ox, ex) + .5) * sx - .5, 0, lw - 1)
                xi = np.floor(xs).astype(np.int64)
                x0, x1 = int(xi[0]), min(lw, int(xi[-1]) + 2)
                if rgb:
                    block = np.zeros((3, y1 - y0, x1 - x0), dtype=arr.dtype)
                    for channel in plan.channels:
                        block[channel] = np.asarray(arr[channel, y0:y1, x0:x1])
                    pixels = render.composite(block, list(plan.channels), windows, colors, True, gammas)
                else:
                    block = np.stack([np.asarray(arr[c, y0:y1, x0:x1]) for c in plan.channels])
                    pixels = render.composite(block, list(range(len(plan.channels))),
                                              [windows[c] for c in plan.channels],
                                              [colors[c] for c in plan.channels], False,
                                              [gammas[c] for c in plan.channels])
                xx, yy = xi - x0, yi - y0
                xx1, yy1 = np.minimum(xx + 1, x1 - x0 - 1), np.minimum(yy + 1, y1 - y0 - 1)
                ax = (xs - xi).astype(np.float32)[None, :, None]
                ay = (ys - yi).astype(np.float32)[:, None, None]
                top = pixels[yy[:, None], xx] * (1 - ax) + pixels[yy[:, None], xx1] * ax
                bottom = pixels[yy1[:, None], xx] * (1 - ax) + pixels[yy1[:, None], xx1] * ax
                sampled = np.rint(top * (1 - ay) + bottom * ay).clip(0, 255).astype(np.uint8)
                with Image.fromarray(sampled) as patch:
                    output.paste(patch, (ox, oy))
        return output
    except BaseException:
        output.close()
        raise


def navigation_center(box):
    # Match the reference's integer centre of inclusive source endpoints.
    return (box.x1 + box.x2) // 2, (box.y1 + box.y2) // 2


def crop_bounds(cx, cy, width, height, source_size):
    width, height = min(width, source_size[0]), min(height, source_size[1])
    x = max(0, min(source_size[0] - width, round(cx - width / 2)))
    y = max(0, min(source_size[1] - height, round(cy - height / 2)))
    return x, y, x + width, y + height


def fit_panel(size, panel):
    x, top, width, height = panel
    scale = min(width / size[0], height / size[1])
    w, h = size[0] * scale, size[1] * scale
    return x + (width - w) / 2, top + (height - h) / 2, w, h


def map_point(x, y, bounds, panel):
    px, top, width, height = panel
    left, upper, right, bottom = bounds
    return (px + (x - left) / (right - left) * width,
            top + (y - upper) / (bottom - upper) * height)


def _region(bounds):
    left, top, right, bottom = bounds
    return left, top, right - left, bottom - top


def _jpeg(image, quality=91):
    data = io.BytesIO()
    image.save(data, 'JPEG', quality=quality, subsampling=0, optimize=True)
    return data.getvalue()


def _place_image(c, jpeg, panel):
    x, top, w, h = panel
    c.drawImage(ImageReader(io.BytesIO(jpeg)), x, PAGE_H - top - h, width=w, height=h)


class _Scene:
    """Collect a page's few vector marks before applying all label clearances."""
    def __init__(self):
        self.paths, self.labels = [], []

    def rect(self, x, top, w, h, color, width=1.4):
        if w > 0 and h > 0:
            self.paths.append(('rect', (x, top, w, h), color, width))

    def line(self, x1, y1, x2, y2, color, width=1):
        self.paths.append(('line', (x1, y1, x2, y2), color, width))

    def polygon(self, points, color):
        self.paths.append(('polygon', points, color, 0))

    def label(self, text, x, top, color, size=12):
        self.labels.append((text, x, top, color, size))

    def draw(self, c):
        font = _fonts()[1]
        guards = [label_rect(text, x, top, size, font, PAGE_H, gap=4.0)
                  for text, x, top, _, size in self.labels]
        with clip_label_paths(c, PAGE_POINTS, guards):
            for kind, values, color, width in self.paths:
                c.setStrokeColorRGB(*color)
                c.setFillColorRGB(*color)
                c.setLineWidth(width)
                if kind == 'rect':
                    x, top, w, h = values
                    c.rect(x, PAGE_H - top - h, w, h, stroke=1, fill=0)
                elif kind == 'line':
                    x1, y1, x2, y2 = values
                    c.line(x1, PAGE_H - y1, x2, PAGE_H - y2)
                else:
                    path = c.beginPath()
                    for i, (x, y) in enumerate(values):
                        (path.moveTo if i == 0 else path.lineTo)(x, PAGE_H - y)
                    path.close()
                    c.drawPath(path, fill=1, stroke=0)
        for text, x, top, color, size in self.labels:
            c.setFillColorRGB(*color)
            obj = c.beginText(x, PAGE_H - top - size)
            obj.setFont(font, size)
            obj.setTextRenderMode(0)
            obj.textOut(text)
            c.drawText(obj)


def _box(scene, center, bounds, panel, color, width=1.5):
    cx, cy = center
    left, top, right, bottom = bounds
    x1, y1 = max(left, cx - BOX / 2), max(top, cy - BOX / 2)
    x2, y2 = min(right, cx + BOX / 2), min(bottom, cy + BOX / 2)
    x, y = map_point(x1, y1, bounds, panel)
    xx, yy = map_point(x2, y2, bounds, panel)
    scene.rect(x, y, xx - x, yy - y, color, width)
    return x, y, xx - x, yy - y


def _visible(center, bounds):
    x, y = center
    left, top, right, bottom = bounds
    return left <= x <= right and top <= y <= bottom


def _leader_label(scene, center, number, bounds, panel, color, role, occupied):
    x, y, w, h = _box(scene, center, bounds, panel, color, 1.5 if role == 'current' else 1.0)
    text = f'#{number}'
    text_w = pdfmetrics.stringWidth(text, _fonts()[1], 12)
    candidates = {
        'current': [(x+w+34,y-31), (x+w+34,y+h+19), (x-text_w-34,y-31)],
        'previous': [(x-text_w-36,y-35), (x-text_w-36,y+h+19), (x+w+30,y-55)],
        'next': [(x+w+34,y+h+24), (x-text_w-34,y+h+24), (x+w+34,y-54)],
    }[role]
    px, pt, pw, ph = panel
    possible = []
    for lx, ly in candidates:
        lx = max(px+10, min(px+pw-text_w-10, lx))
        ly = max(pt+10, min(pt+ph-24, ly))
        rect = (lx-4, ly-3, lx+text_w+4, ly+17)
        hits = sum(not(rect[2] < o[0] or rect[0] > o[2] or rect[3] < o[1] or rect[1] > o[3]) for o in occupied)
        possible.append((hits, lx, ly, rect))
    _, lx, ly, rect = min(possible, key=lambda v: v[0])
    occupied.append(rect)
    ax, ay = min(max(lx+text_w/2, x), x+w), min(max(ly+6, y), y+h)
    tx, ty = lx+text_w/2, ly+8
    enter = 0.0
    for origin, delta, lo, hi in ((ax, tx-ax, rect[0], rect[2]), (ay, ty-ay, rect[1], rect[3])):
        if abs(delta) > 1e-12:
            enter = max(enter, min((lo-origin)/delta, (hi-origin)/delta))
    enter = max(0.0, min(1.0, enter))
    scene.line(ax, ay, ax+(tx-ax)*enter, ay+(ty-ay)*enter, color, .8)
    scene.label(text, lx, ly, color)


def _next_arrow(scene, current, nxt, number, bounds, panel):
    px, pt, pw, ph = panel
    sx, sy = map_point(*current, bounds, panel)
    tx, ty = map_point(*nxt, bounds, panel)
    dx, dy = tx-sx, ty-sy
    norm = math.hypot(dx, dy)
    if norm == 0:
        return
    dx, dy = dx/norm, dy/norm
    limits = []
    if dx > 0:
        limits.append((px+pw-28-sx)/dx)
    if dx < 0:
        limits.append((px+28-sx)/dx)
    if dy > 0:
        limits.append((pt+ph-28-sy)/dy)
    if dy < 0:
        limits.append((pt+28-sy)/dy)
    nonnegative = [distance for distance in limits if distance >= 0]
    if not nonnegative:
        return
    distance = min(nonnegative)
    ex, ey = sx+dx*distance, sy+dy*distance
    bx, by = ex-dx*57, ey-dy*57
    scene.line(bx, by, ex-dx*13, ey-dy*13, CYAN, 5.5)
    scene.polygon(((ex,ey), (ex-dx*17-dy*9,ey-dy*17+dx*9),
                   (ex-dx*17+dy*9,ey-dy*17-dx*9)), CYAN)
    label = f'Next #{number}'
    tw = pdfmetrics.stringWidth(label, _fonts()[1], 10)
    lx = max(px+10, min(px+pw-tw-10, bx-tw/2))
    ly = max(pt+10, min(pt+ph-24, by+10 if dy < 0 else by-24))
    scene.label(label, lx, ly, CYAN, 10)


def _text(c, text, x, top, size=11, bold=False, color=(0,0,0)):
    c.setFillColorRGB(*color)
    c.setFont(_text_font(text, bold), size)
    c.drawString(x, PAGE_H-top-size, text)


def _text_lines(text, width, size, max_lines, bold=False):
    text = str(text).replace('\r', ' ').replace('\n', ' ')
    font = _text_font(text, bold)
    lines, line = [], ''
    for char in text:
        if line and pdfmetrics.stringWidth(line+char, font, size) > width:
            lines.append(line.rstrip())
            line = ''
            if len(lines) == max_lines:
                break
        line += char
    else:
        if line or not lines:
            lines.append(line.rstrip())
        return lines
    while lines[-1] and pdfmetrics.stringWidth(lines[-1]+'...', font, size) > width:
        lines[-1] = lines[-1][:-1]
    lines[-1] += '...'
    return lines


def render_page(plan, index, whole_jpeg):
    """Generate one small PDF with independent images and vector annotations."""
    current = plan.source_boxes[index]
    center, source_size = navigation_center(current), _source_size(plan)
    detail_bounds = crop_bounds(*center, DETAIL_SPAN, DETAIL_SPAN, source_size)
    context_bounds = crop_bounds(*center, CONTEXT_W, CONTEXT_H, source_size)
    detail_panel = fit_panel(_region(detail_bounds)[2:], DETAIL)
    context_panel = fit_panel(_region(context_bounds)[2:], RIGHT)
    whole_panel = fit_panel(source_size, WHOLE)
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=PAGE_POINTS, pageCompression=1)
    with render_region(plan, _region(detail_bounds), (460,460)) as image:
        _place_image(c, _jpeg(image), detail_panel)
    _place_image(c, whole_jpeg, whole_panel)
    with render_region(plan, _region(context_bounds), (1530,1013)) as image:
        _place_image(c, _jpeg(image), context_panel)
    _text(c, f'ROI {index+1}', 12, 20, 26, bold=True)
    source_name = Path(str(plan.attrs['nd2wsi'].get('source', plan.stem)).replace('\\','/')).stem
    source_lines = _text_lines(source_name, 132, 13, 2, bold=True)
    for row, line in enumerate(source_lines):
        _text(c, line, 13, 68+row*16, 13, bold=True)
    note_top = 89 if len(source_lines) == 1 else 108
    for row, line in enumerate(_text_lines(current.note, 132, 11, 4)):
        _text(c, line, 13, note_top+row*13, 11)
    _text(c, f'{index+1} / {len(plan.source_boxes)}', 13, 175, 10, color=(.3,.3,.3))
    scene = _Scene()
    _box(scene, center, detail_bounds, detail_panel, YELLOW)
    scene.label(f'#{index+1}', detail_panel[0]+8, detail_panel[1]+7, YELLOW, 11)
    x, y = map_point(context_bounds[0], context_bounds[1], (0,0,*source_size), whole_panel)
    xx, yy = map_point(context_bounds[2], context_bounds[3], (0,0,*source_size), whole_panel)
    scene.rect(x, y, xx-x, yy-y, YELLOW, 1.1)
    _box(scene, center, (0,0,*source_size), whole_panel, YELLOW, .8)
    occupied = []
    for ni, role in ((index-1,'previous'), (index+1,'next')):
        if 0 <= ni < len(plan.source_boxes):
            neighbor = navigation_center(plan.source_boxes[ni])
            if _visible(neighbor, context_bounds):
                _leader_label(scene, neighbor, ni+1, context_bounds, context_panel, GREY, role, occupied)
    _leader_label(scene, center, index+1, context_bounds, context_panel, YELLOW, 'current', occupied)
    if index+1 < len(plan.source_boxes):
        nxt = navigation_center(plan.source_boxes[index+1])
        if not _visible(nxt, context_bounds):
            _next_arrow(scene, center, nxt, index+2, context_bounds, context_panel)
    scene.rect(.25, .25, PAGE_W-.5, PAGE_H-.5, (0,0,0), .35)
    scene.draw(c)
    c.showPage()
    c.save()
    return buffer.getvalue()


def write_pdf(plan, jpg: Path, destination: Path):
    """Write SG-layout pages while retaining only one page's image buffers."""
    from .astromap_export import AstromapError

    if not plan.source_boxes:
        raise AstromapError('PDF export requires at least one source ROI')
    if 1530 * 1013 * 32 > render._available_memory() // 2:
        raise AstromapError('Not enough free memory for the ROI PDF; close other slides and retry')
    with PDF_LOCK:
        _fonts()
        source_size = _source_size(plan)
        # Sample a clean overview once. The JPG exported beside the PDF already
        # contains boxes; reusing it would bake unrelated yellow boxes into maps.
        with render_region(plan, (0,0,*source_size), (1300,1000)) as overview:
            whole_jpeg = _jpeg(overview, 90)
        with StreamingPdf(destination) as pdf:
            for index in range(len(plan.source_boxes)):
                pdf.append(render_page(plan, index, whole_jpeg), f'ROI {index+1}')
            pdf.finish(title=f'{plan.stem} - ROI navigation')
