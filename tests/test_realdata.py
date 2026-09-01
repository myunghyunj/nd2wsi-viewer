"""Tests against the real Ti2 acquisitions from the testdata release.

Run ``python scripts/fetch_testdata.py`` first; without the files these
skip. Select with ``pytest -m realdata``.
"""

from pathlib import Path

import numpy as np
import pytest

nd2 = pytest.importorskip("nd2")

DOCS = Path(__file__).resolve().parent.parent / "docs"
CELL = DOCS / "example_cell.nd2"
TISSUE = DOCS / "example_tissue.nd2"

pytestmark = [
    pytest.mark.realdata,
    pytest.mark.skipif(
        not (CELL.exists() and TISSUE.exists()),
        reason="example acquisitions not fetched (scripts/fetch_testdata.py)",
    ),
]


@pytest.fixture(scope="module", params=["cell", "tissue"])
def slide(request, tmp_path_factory):
    src = CELL if request.param == "cell" else TISSUE
    work = tmp_path_factory.mktemp(request.param)
    copy = work / src.name
    copy.write_bytes(src.read_bytes())  # keep caches out of docs/
    return copy


def test_real_slide_converts_and_reads_back(slide):
    import zarr

    from nd2wsi.convert import convert, default_store_path

    store = default_store_path(slide)
    convert(slide, store, progress=False)
    root = zarr.open_group(str(store), mode="r")
    attrs = dict(root.attrs)
    meta = attrs["nd2wsi"]

    with nd2.ND2File(str(slide)) as f:
        c = f.sizes["C"]
        h, w = f.sizes["Y"], f.sizes["X"]
        vs = f.voxel_size()
    lv0 = meta["levels"][0]
    assert (lv0["height"], lv0["width"]) == (h, w)
    assert len(attrs["omero"]["channels"]) == c

    # the file's own calibration, not an invented one
    py, px = meta["pixel_size_um"]
    assert abs(px - vs.x) < 1e-6 and abs(py - vs.y) < 1e-6
    assert 0.5 < px < 1.0  # these scans are 0.66 um/px


def test_real_slide_roi_roundtrip_is_pixel_exact(slide):
    from nd2wsi.convert import default_store_path
    from nd2wsi.server import SlideRegistry

    registry = SlideRegistry()
    sid = registry.open_path(slide)
    st = registry.slides[sid]
    arr = st.root["0"]

    with nd2.ND2File(str(slide)) as f:
        # read_frame hands back a view over the file's memory map, which
        # dies with the file — materialize the region before leaving the block
        frame = f.read_frame(0)
        if frame.ndim == 3 and frame.shape[-1] == arr.shape[0]:
            frame = np.moveaxis(frame, -1, 0)  # (Y, X, C) -> (C, Y, X)
        ref = np.array(frame[:, 500:900, 600:1100])

    got = np.asarray(arr[:, 500:900, 600:1100])
    assert (got == ref).all()
    registry.remove(sid)
    assert default_store_path(slide).exists()


def test_compact_cache_serves_the_source_and_saves_space(tmp_path):
    from nd2wsi.cache import read_manifest
    from nd2wsi.convert import convert, ensure_cache
    from nd2wsi.server import SlideRegistry

    copy = tmp_path / CELL.name
    copy.write_bytes(CELL.read_bytes())
    store = ensure_cache(copy)
    assert read_manifest(store.parent)["kind"] == "overview"

    full = tmp_path / "full.ome.zarr"
    convert(copy, full, progress=False)

    def du(p):
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())

    # levels 1..n hold a third of a full pyramid's pixels
    assert du(store) < 0.45 * du(full)

    reg = SlideRegistry()
    sid = reg.add_store(store)
    st = reg.get(sid)
    assert st.attrs["nd2wsi"]["kind"] == "source-backed"
    with nd2.ND2File(str(copy)) as f:
        frame = f.read_frame(0)
        if frame.ndim == 3 and frame.shape[-1] == st.root["0"].shape[0]:
            frame = np.moveaxis(frame, -1, 0)
        ref = np.array(frame[:, 100:400, 200:600])
    assert np.array_equal(st.root["0"][:, 100:400, 200:600], ref)
    reg.remove(sid)
