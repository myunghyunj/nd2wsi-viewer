"""SG label clearance protects vector paths without altering images or text."""
from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest

from nd2wsi.pdf_label_clearance import clip_label_paths, label_rect


def _font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    name = "ClearanceTestFont"
    if name not in pdfmetrics.getRegisteredFontNames():
        import reportlab

        path = Path(reportlab.__file__).parent / "fonts" / "VeraBd.ttf"
        pdfmetrics.registerFont(TTFont(name, str(path)))
    return name


def test_label_rectangle_matches_embedded_font_descriptor():
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas

    font = _font()
    output = io.BytesIO()
    c = canvas.Canvas(output, pagesize=(400, 300))
    c.setFont(font, 12)
    c.drawString(80, 300 - 50 - 12, "Next #123")
    c.save()
    page = PdfReader(output).pages[0]
    labels = []

    def visit(text, cm, tm, embedded, size):
        if text.strip() != "Next #123":
            return
        first = int(embedded["/FirstChar"])
        widths = embedded["/Widths"]
        width = sum(float(widths[ord(ch) - first]) for ch in text.strip()) * size / 1000
        desc = embedded["/FontDescriptor"].get_object()
        bottom = tm[5] + float(desc["/Descent"]) * size / 1000 - 4
        top = tm[5] + float(desc["/Ascent"]) * size / 1000 + 4
        labels.append((tm[4] - 4, bottom, width + 8, top - bottom))

    page.extract_text(visitor_text=visit)
    assert len(labels) == 1
    assert label_rect("Next #123", 80, 50, 12, font, 300) == pytest.approx(labels[0], abs=1e-5)


def _document(protect):
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    output = io.BytesIO()
    c = canvas.Canvas(output, pagesize=(200, 120), pageCompression=0)
    font = _font()
    # Full-bleed image makes opaque masking or accidentally clipped image draws
    # detectable. Two overlapping label clearances exercise XOR cancellation.
    image = Image.new("RGB", (20, 12), (20, 40, 80))
    try:
        c.drawImage(ImageReader(image), 0, 0, width=200, height=120)
    finally:
        image.close()
    labels = [("#123", 60, 35), ("#124", 78, 35)]
    holes = [label_rect(text, x, top, 12, font, 120) for text, x, top in labels]

    def vectors():
        c.setStrokeColorRGB(1, 1, 0)
        c.setLineWidth(3)
        c.line(0, 74, 200, 74)
        c.setFillColorRGB(0, 1, 1)
        path = c.beginPath()
        path.moveTo(70, 60)
        path.lineTo(120, 60)
        path.lineTo(95, 100)
        path.close()
        c.drawPath(path, stroke=0, fill=1)

    if protect:
        with clip_label_paths(c, (200, 120), holes):
            vectors()
    else:
        vectors()
    c.setFont(font, 12)
    for text, x, top in labels:
        c.drawString(x, 120 - top - 12, text)
    c.bookmarkPage("ROI_1")
    c.addOutlineEntry("ROI 1", "ROI_1", 0)
    c.save()
    return output.getvalue(), holes


def test_every_vector_path_is_guarded_with_separate_intersecting_holes():
    from pypdf import PdfReader
    from pypdf.generic import ContentStream

    data, holes = _document(True)
    reader = PdfReader(io.BytesIO(data))
    operations = ContentStream(reader.pages[0].get_contents(), reader).operations
    clips = [(args, op) for args, op in operations if op == b"W*"]
    assert len(clips) == len(holes) == 2
    painted = {b"S", b"s", b"f", b"f*", b"B", b"B*", b"b", b"b*"}
    clip_counts = [0]
    paths = []
    for args, op in operations:
        if op == b"q":
            clip_counts.append(clip_counts[-1])
        elif op == b"Q":
            clip_counts.pop()
        elif op == b"W*":
            clip_counts[-1] += 1
        elif op in painted:
            assert clip_counts[-1] == len(holes)
            paths.append(op)
        elif op in {b"Do", b"Tj", b"TJ"}:
            assert clip_counts[-1] == 0, "Image/text must not inherit vector-only clips"
    assert paths == [b"S", b"f*"]
    assert clip_counts == [0]


def test_images_text_and_outline_match_the_unprotected_document():
    from pypdf import PdfReader

    def inspect(data):
        reader = PdfReader(io.BytesIO(data))
        page = reader.pages[0]
        images = []
        for ref in page["/Resources"]["/XObject"].values():
            obj = ref.get_object()
            if obj["/Subtype"] == "/Image":
                images.append((obj["/Width"], obj["/Height"], obj._data))
        return page.extract_text(), images, [item["/Title"] for item in reader.outline]

    assert inspect(_document(True)[0]) == inspect(_document(False)[0])


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="Poppler is not installed")
def test_overlapping_label_clearances_reveal_original_image_not_xor_or_opaque_mask(tmp_path):
    from PIL import Image

    pdf = tmp_path / "clearance.pdf"
    pdf.write_bytes(_document(True)[0])
    subprocess.run([shutil.which("pdftoppm"), "-r", "72", "-singlefile", "-png",
                    str(pdf), str(tmp_path / "clearance")], check=True, capture_output=True)
    with Image.open(tmp_path / "clearance.png") as raster:
        # This point is inside BOTH label holes and beneath the cyan triangle,
        # but below glyphs. XOR hole cancellation would expose cyan here.
        assert raster.getpixel((90, 120 - 68)) == (20, 40, 80)
        # The same horizontal vector outside label clearances stays visible.
        assert raster.getpixel((30, 120 - 74)) == (255, 255, 0)


class CanvasSpy:
    def __init__(self):
        self.depth = 0

    def saveState(self):
        self.depth += 1

    def restoreState(self):
        self.depth -= 1

    def addLiteral(self, value):
        pass


def test_graphics_state_restores_after_drawing_failure():
    c = CanvasSpy()
    with pytest.raises(RuntimeError, match="draw failure"):
        with clip_label_paths(c, (200, 120), []):
            assert c.depth == 1
            raise RuntimeError("draw failure")
    assert c.depth == 0


@pytest.mark.parametrize("page,holes", [((0, 120), []), ((200, 120), [(0, 0, -1, 10)]),
                                        ((200, 120), [(0, 0, 10, float("inf"))])])
def test_invalid_geometry_fails_before_changing_canvas_state(page, holes):
    c = CanvasSpy()
    with pytest.raises(ValueError):
        with clip_label_paths(c, page, holes):
            pytest.fail("Invalid clipping geometry was accepted")
    assert c.depth == 0
