"""SG navigation atlas: exact layout, selectable labels and bounded reads."""
import re
import zipfile

import numpy as np
import pytest
from PIL import Image, PdfParser

from nd2wsi import astromap_export as am
from nd2wsi import astromap_pdf as atlas
from nd2wsi.server import ViewerState, _frame_args


def make_state(*, width=80, height=60, rgb=False, root=None, levels=None):
    """A three-channel slide; by default one level of flat 40/100/160 planes."""
    levels = levels or [{"path": "0", "width": width, "height": height}]
    channels = [
        {"label": name, "color": color, "window": {"start": 0, "end": 255}}
        for name, color in zip(("R", "G", "B"), ("FF0000", "00FF00", "0000FF"))
    ]
    attrs = {"nd2wsi": {"source": "example.nd2", "levels": levels, "rgb": rgb},
             "omero": {"channels": channels}}
    if root is None:
        root = {levels[0]["path"]: np.stack([
            np.full((height, width), value, np.uint8) for value in (40, 100, 160)
        ])}
    return ViewerState(root, attrs, generation="current")


def prepare(st=None, **changes):
    payload = {"generation": "current", "rois": [{"x": 5, "y": 6, "w": 12, "h": 9, "note": "A"}],
               "channels": [0, 1, 2], "window": None, "frame": None} | changes
    return am.prepare_astromap(st or make_state(), payload, resolve_frame=_frame_args)


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_pdf_option_rejects_non_boolean_values_before_render(value):
    with pytest.raises(am.AstromapError, match="include_pdf must be a boolean"):
        prepare(include_pdf=value)


def test_optional_pdf_keeps_jpg_txt_identical_and_has_one_sg_page_per_roi(tmp_path):
    rois = [{"x": 5, "y": 6, "w": 12, "h": 9, "note": "검증 ROI α"},
            {"x": 60, "y": 45, "w": 10, "h": 8, "note": "Second region"}]
    plain_dir, pdf_dir = tmp_path / "plain", tmp_path / "pdf"
    plain_dir.mkdir()
    pdf_dir.mkdir()
    ordinary = prepare(rois=rois).write(plain_dir)
    optional = prepare(rois=rois, include_pdf=True).write(pdf_dir)
    with zipfile.ZipFile(ordinary) as plain, zipfile.ZipFile(optional) as extra:
        assert plain.namelist() == ["example.jpg", "example.txt"]
        assert extra.namelist() == ["example.jpg", "example.txt", "example_rois.pdf"]
        for filename in plain.namelist():
            assert plain.read(filename) == extra.read(filename)
        pdf_path = tmp_path / "atlas.pdf"
        pdf_path.write_bytes(extra.read("example_rois.pdf"))
    with PdfParser.PdfParser(filename=str(pdf_path)) as pdf:
        assert len(pdf.pages) == 2
        for reference in pdf.pages:
            page = pdf.read_indirect(reference)
            assert page[b"MediaBox"] == pytest.approx([0, 0, 960, 5391397 / 12700])
    assert [p.name for p in pdf_dir.iterdir()] == ["example_astromap.zip"]


def test_pdf_preserves_source_roi_edges_instead_of_reconstructing_quantized_jpg_bounds():
    st = make_state(width=16001, height=8001, root={})
    plan = prepare(st, channels=[], include_pdf=True,
                   rois=[{"x": 10003, "y": 5001, "w": 3, "h": 2, "note": "tiny"}])
    assert plan.source_boxes == (am.ExportBox(10003, 5001, 10005, 5002, "tiny"),)
    assert plan.boxes != plan.source_boxes


class VirtualArray:
    dtype = np.dtype("uint8")

    def __init__(self, width, height, calls, level):
        self.shape = (3, height, width)
        self.calls, self.level = calls, level

    def __getitem__(self, selection):
        channel, y, x = selection
        assert isinstance(channel, int), "PDF must read only selected channels"
        assert x.stop - x.start <= 512 and y.stop - y.start <= 512
        self.calls.append((self.level, channel, x.stop - x.start, y.stop - y.start))
        return np.full((y.stop - y.start, x.stop - x.start), 120, dtype=np.uint8)


def test_sparse_pyramid_crop_reads_bounded_samples_not_the_entire_region():
    calls = []
    width, height = 1_000_000_001, 500_000_001
    st = make_state(width=width, height=height,
                    root={"0": VirtualArray(width, height, calls, "0")})
    plan = prepare(st, include_pdf=True, channels=[2])
    with atlas.render_region(plan, (0, 0, width, height), (17, 9)) as image:
        assert image.width <= 17 and image.height <= 9
        assert image.getpixel((0, 0)) == (0, 0, 120)
    assert calls and {call[1] for call in calls} == {2}
    assert all(w <= 2 and h <= 2 for _, _, w, h in calls)


def test_pdf_crops_choose_pyramid_resolution_per_panel_and_keep_raw_origin():
    calls = []
    levels = [{"path": str(n), "width": 4096 // (2**n), "height": 2048 // (2**n)}
              for n in range(5)]
    root = {lv["path"]: VirtualArray(lv["width"], lv["height"], calls, lv["path"]) for lv in levels}
    plan = prepare(make_state(width=4096, height=2048, levels=levels, root=root),
                   channels=[1], include_pdf=True)
    with atlas.render_region(plan, (0, 0, 4096, 2048), (256, 128)):
        pass
    assert {c[0] for c in calls} == {"4"}
    calls.clear()
    with atlas.render_region(plan, (1000, 500, 40, 20), (256, 128)):
        pass
    assert {c[0] for c in calls} == {"0"}


def test_sg_template_fields_are_fixed_native_pixel_spans():
    assert atlas.PAGE_POINTS == pytest.approx((960, 5391397 / 12700))
    assert (atlas.BOX, atlas.DETAIL_SPAN, atlas.CONTEXT_W, atlas.CONTEXT_H) == (300, 1000, 3000, 1984)


@pytest.mark.parametrize("box,expected", [
    (am.ExportBox(100, 200, 399, 499, "even widths"), (249, 349)),
    (am.ExportBox(0, 0, 0, 0, "one pixel"), (0, 0)),
    (am.ExportBox(10003, 5001, 10005, 5002, "odd source"), (10004, 5001)),
])
def test_navigation_centres_match_reference_inclusive_endpoint_floor(box, expected):
    assert atlas.navigation_center(box) == expected


@pytest.mark.parametrize("centre,span,source,expected", [
    ((1000, 1200), (1000, 1000), (6000, 4000), (500, 700, 1500, 1700)),
    ((0, 0), (3000, 1984), (6000, 4000), (0, 0, 3000, 1984)),
    ((5999, 3999), (3000, 1984), (6000, 4000), (3000, 2016, 6000, 4000)),
    ((79, 59), (1000, 1000), (80, 60), (0, 0, 80, 60)),
])
def test_crops_shift_inside_source_without_stretching_or_negative_bounds(centre, span, source, expected):
    assert atlas.crop_bounds(*centre, *span, source) == expected


def test_pdf_source_render_matches_identity_rgb_samples_and_selected_lut():
    data = np.arange(3 * 60 * 80, dtype=np.uint8).reshape(3, 60, 80)
    st = make_state(width=80, height=60, rgb=True, root={"0": data})
    plan = prepare(st, include_pdf=True, channels=[0, 2])
    with atlas.render_region(plan, (10, 20, 12, 9), (12, 9)) as image:
        pixels = np.array(image)
    np.testing.assert_array_equal(pixels[:, :, 0], data[0, 20:29, 10:22])
    np.testing.assert_array_equal(pixels[:, :, 1], 0)
    np.testing.assert_array_equal(pixels[:, :, 2], data[2, 20:29, 10:22])


@pytest.fixture
def navigation_pdf(tmp_path):
    """Nearby and distant next ROIs exercise both navigation modes cheaply."""
    st = make_state(width=6000, height=4000, root={})
    st.attrs["nd2wsi"]["pixel_size_um"] = [.4, .2]
    plan = prepare(st, channels=[], include_pdf=True, rois=[
        {"x": 900, "y": 900, "w": 200, "h": 200, "note": "검증 ROI α"},
        {"x": 1100, "y": 1000, "w": 100, "h": 120, "note": "Nearby region"},
        {"x": 5100, "y": 3100, "w": 50, "h": 80, "note": "Distant region"},
    ])
    before = plan.source_boxes
    jpg, pdf = tmp_path / "small.jpg", tmp_path / "navigation.pdf"
    with Image.new("RGB", (150, 100), (20, 30, 40)) as image:
        image.save(jpg, "JPEG")
    atlas.write_pdf(plan, jpg, pdf)
    assert plan.source_boxes == before
    return pdf


def test_pdf_text_bookmarks_and_navigation_roles_match_sg_reference(navigation_pdf):
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(navigation_pdf)
    assert len(reader.pages) == 3
    texts = [page.extract_text() for page in reader.pages]
    for i, text in enumerate(texts, 1):
        assert f"ROI {i}" in text and f"{i} / 3" in text
        assert f"#{i}" in text
        assert not any(unit in text for unit in ("µm", "μm", " px", "Scale", "scale bar"))
    assert "검증 ROI α" in texts[0]
    assert "#2" in texts[0] and "Next #2" not in texts[0]
    assert "#1" in texts[1] and "Next #3" in texts[1]
    assert "Next #" not in texts[2]

    def titles(items):
        for item in items:
            if isinstance(item, list):
                yield from titles(item)
            else:
                yield item.title

    outline = list(titles(reader.outline))
    assert all(f"ROI {i}" in outline for i in range(1, 4))


def test_images_and_filled_text_stay_outside_transparent_vector_clearance(navigation_pdf):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import ContentStream

    reader = pypdf.PdfReader(navigation_pdf)
    paint = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}
    for page in reader.pages:
        label_count = sum(bool(re.fullmatch(r"(?:Next )?#\d+", line.strip()))
                          for line in page.extract_text().splitlines())
        assert label_count >= 2
        clips, stack, paths, images, text_runs = 0, [], 0, 0, 0
        for args, op in ContentStream(page.get_contents(), reader).operations:
            if op == b"q":
                stack.append(clips)
            elif op == b"Q":
                clips = stack.pop()
            elif op == b"W*":
                clips += 1
            elif op in paint:
                # Every stroke/fill, including the page border, must avoid
                # every padded number. Multiple W* calls intersect holes.
                assert clips == label_count, "An unprotected vector stroke/fill remains"
                paths += 1
            elif op == b"Do":
                assert clips == 0, "Label clearance must not erase image pixels"
                images += 1
            elif op in {b"Tj", b"TJ", b"'", b'"'}:
                assert clips == 0, "Label text itself must not be clipped"
                text_runs += 1
            elif op == b"Tr":
                assert int(args[0]) == 0, "Labels must be fill-only without text outlines"
        assert not stack and clips == 0
        assert paths >= 4 and images == 3 and text_runs >= 4
