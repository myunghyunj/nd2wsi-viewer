"""One-page PDF imports preserve vectors/fonts while releasing image graphs."""
from __future__ import annotations

import gc
import hashlib
import io
import shutil
import subprocess
import weakref
from pathlib import Path

import pypdf
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream, IndirectObject, NameObject
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from nd2wsi import pdf_stream
from nd2wsi.pdf_stream import StreamingPdf


def jpeg(color=(15, 90, 170)):
    output = io.BytesIO()
    with Image.new("RGB", (32, 24), color) as image:
        image.save(output, "JPEG", quality=90)
    return output.getvalue()


def page_pdf(number=1, image_bytes=None, font="Helvetica", text=None):
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(320, 200), pageCompression=1)
    pdf.setFont(font, 15)
    pdf.drawString(15, 175, text or f"ROI {number}")
    pdf.setStrokeColorRGB(1, 1, 0)
    pdf.setLineWidth(2)
    pdf.rect(10, 12, 250, 140)
    pdf.drawImage(ImageReader(io.BytesIO(image_bytes or jpeg())), 20, 30, width=128, height=96)
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def image_objects(page):
    return [obj for obj in page["/Resources"]["/XObject"].values()
            if obj.get_object().get("/Subtype") == "/Image"]


def test_two_pages_preserve_content_text_images_references_and_unicode_bookmarks(tmp_path):
    source = [page_pdf(1), page_pdf(2)]
    destination = tmp_path / "streamed.pdf"
    with StreamingPdf(destination) as output:
        for i, data in enumerate(source, 1):
            output.append(data, f"ROI {i} - 검증")
        assert output.page_count == 2
        assert output.image_count == 1
        output.finish(title="한글 PDF / ROI navigation")
    reader = PdfReader(destination, strict=True)
    assert reader.metadata.title == "한글 PDF / ROI navigation"
    assert len(reader.pages) == len(reader.outline) == 2
    assert [item.title for item in reader.outline] == ["ROI 1 - 검증", "ROI 2 - 검증"]
    assert [reader.get_destination_page_number(item) for item in reader.outline] == [0, 1]
    assert image_objects(reader.pages[0])[0].idnum == image_objects(reader.pages[1])[0].idnum
    assert reader.trailer["/Root"]["/Pages"].indirect_reference.idnum == 2
    for index, page in enumerate(reader.pages):
        original = PdfReader(io.BytesIO(source[index])).pages[0]
        assert page.extract_text() == original.extract_text()
        assert tuple(page.mediabox) == (0, 0, 320, 200)
        assert page.raw_get("/Parent").idnum == 2
        assert page.get_contents().get_data() == original.get_contents().get_data()
        assert image_objects(page)[0].get_object()._data == image_objects(original)[0].get_object()._data
        assert any(operator == b"re" for _, operator in ContentStream(page.get_contents(), reader).operations)
    # Resolve every emitted object, not only the page/catalog happy path.
    for number in reader.xref[0]:
        assert reader.get_object(IndirectObject(number, 0, reader)) is not None


def test_distinct_image_attributes_are_not_deduplicated_by_jpeg_bytes_alone(tmp_path):
    first = page_pdf()
    changed = PdfReader(io.BytesIO(first))
    image_objects(changed.pages[0])[0].get_object()[NameObject("/Interpolate")] = pypdf.generic.BooleanObject(True)
    writer = PdfWriter()
    writer.add_page(changed.pages[0])
    second = io.BytesIO()
    writer.write(second)
    destination = tmp_path / "attributes.pdf"
    with StreamingPdf(destination) as output:
        output.append(first, "original")
        output.append(second.getvalue(), "interpolated")
        assert output.image_count == 2
    reader = PdfReader(destination)
    a, b = [image_objects(page)[0] for page in reader.pages]
    assert a.idnum != b.idnum
    assert a.get_object()._data == b.get_object()._data
    assert "/Interpolate" not in a.get_object() and b.get_object()["/Interpolate"]


def test_embedded_font_and_unicode_text_survive_object_import(tmp_path):
    fonts = [Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
             Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    path = next((font for font in fonts if font.exists()), None)
    if path is None:
        pytest.skip("No local TrueType font")
    pdfmetrics.registerFont(TTFont("StreamingTestFont", str(path)))
    original = page_pdf(font="StreamingTestFont", text="ROI 3 - μm αβ")
    destination = tmp_path / "font.pdf"
    with StreamingPdf(destination) as output:
        output.append(original, "ROI 3")
    before = PdfReader(io.BytesIO(original)).pages[0]
    after = PdfReader(destination).pages[0]
    assert after.extract_text() == before.extract_text() == "ROI 3 - μm αβ\n"
    fonts = [ref.get_object() for ref in after["/Resources"]["/Font"].values()]
    embedded = next(font for font in fonts if "/FontDescriptor" in font)
    assert embedded["/FontDescriptor"]["/FontFile2"].get_data()
    assert embedded["/ToUnicode"].get_data()


def test_append_releases_reader_and_encoded_page_buffer(monkeypatch, tmp_path):
    observed = []
    real_reader = pdf_stream.PdfReader

    def recording_reader(stream, *args, **kwargs):
        reader = real_reader(stream, *args, **kwargs)
        observed.append((weakref.ref(reader), stream))
        return reader

    monkeypatch.setattr(pdf_stream, "PdfReader", recording_reader)
    destination = tmp_path / "released.pdf"
    with StreamingPdf(destination) as output:
        for i in range(12):
            output.append(page_pdf(i), f"ROI {i}")
            gc.collect()
            assert all(ref() is None and stream.closed for ref, stream in observed)
        assert output.image_count == 1
        # Persisted metadata is IDs/offsets/titles/hashes, never PDF objects.
        assert all(isinstance(k, bytes) and isinstance(v, int) for k, v in output._images.items())
        assert all(isinstance(number, int) and isinstance(title, str) for number, title in output._pages)


def test_invalid_multipage_input_removes_partial_file(tmp_path):
    writer = PdfWriter()
    source = PdfReader(io.BytesIO(page_pdf()))
    writer.add_page(source.pages[0])
    writer.add_page(source.pages[0])
    data = io.BytesIO()
    writer.write(data)
    destination = tmp_path / "incomplete.pdf"
    with pytest.raises(ValueError, match="exactly one"):
        with StreamingPdf(destination) as output:
            output.append(data.getvalue(), "invalid")
    assert not destination.exists()


def test_rendered_pages_equal_the_independent_inputs(tmp_path):
    renderer = shutil.which("pdftoppm")
    if renderer is None:
        pytest.skip("Poppler not installed")
    inputs = [page_pdf(1), page_pdf(2, jpeg((170, 60, 30)))]
    destination = tmp_path / "rendered.pdf"
    with StreamingPdf(destination) as output:
        for i, data in enumerate(inputs, 1):
            output.append(data, f"ROI {i}")
    subprocess.run([renderer, "-r", "72", "-png", str(destination), str(tmp_path / "out")],
                   check=True, capture_output=True)
    for index, data in enumerate(inputs, 1):
        source = tmp_path / f"input{index}.pdf"
        source.write_bytes(data)
        prefix = tmp_path / f"in{index}"
        subprocess.run([renderer, "-r", "72", "-png", "-singlefile", str(source), str(prefix)],
                       check=True, capture_output=True)
        with Image.open(prefix.with_suffix(".png")) as before, Image.open(tmp_path / f"out-{index}.png") as after:
            assert before.size == after.size
            assert hashlib.sha256(before.tobytes()).digest() == hashlib.sha256(after.tobytes()).digest()
