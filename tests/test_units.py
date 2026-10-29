"""Fast unit tests that need no ND2 file."""
import json
import os
import stat
import threading
from pathlib import Path

import numpy as np
import pytest

from nd2wsi.convert import build_group_attrs
from nd2wsi.reader import ChannelInfo, PlaneSource, level_shapes
from nd2wsi.render import composite, parse_channels, parse_windows
from nd2wsi.svs import _grid_chunks


def test_level_shapes_halve_until_tile():
    shapes = level_shapes(9600, 12800, 512)
    assert shapes[0] == (9600, 12800)
    assert shapes[-1][0] <= 512 and shapes[-1][1] <= 512
    for (h0, w0), (h1, w1) in zip(shapes, shapes[1:]):
        assert h1 == h0 // 2 and w1 == w0 // 2


def test_level_shapes_small_image_single_level():
    assert level_shapes(100, 200, 512) == [(100, 200)]


def test_grid_chunks():
    assert _grid_chunks(1024, 256) == (256, 256, 256, 256)
    assert _grid_chunks(1000, 256) == (256, 256, 256, 232)
    assert sum(_grid_chunks(998, 256)) == 998


def test_parse_channels():
    assert parse_channels(None, 3) == [0, 1, 2]
    assert parse_channels("1", 3) == [1]
    assert parse_channels("0,2,9", 3) == [0, 2]
    assert parse_channels(",", 3) == [0, 1, 2]


def test_parse_windows():
    defaults = [(0.0, 255.0), (10.0, 90.0)]
    # no override
    assert parse_windows(None, defaults) == (defaults, [1.0, 1.0])
    # override channel 1 only, with gamma
    wins, gammas = parse_windows(",5:50:2", defaults)
    assert wins == [(0.0, 255.0), (5.0, 50.0)] and gammas == [1.0, 2.0]
    # malformed slots fall back to defaults; inverted window is repaired
    wins, gammas = parse_windows("junk,100:100", defaults)
    assert wins[0] == (0.0, 255.0) and wins[1] == (100.0, 101.0)
    # gamma clamped
    _, gammas = parse_windows("0:1:99", defaults)
    assert gammas[0] == 10.0


def test_composite_gamma():
    region = np.array([[[64]]], np.uint8)  # quarter-scale value in a 0-255 window
    linear = composite(region, [0], [(0, 255)], [(255, 255, 255)], rgb=False)
    bright = composite(
        region, [0], [(0, 255)], [(255, 255, 255)], rgb=False, gammas=[2.0]
    )
    assert bright[0, 0, 0] > linear[0, 0, 0]  # gamma > 1 brightens midtones
    assert bright[0, 0, 0] == int((64 / 255) ** 0.5 * 255)  # matches astype truncation


def test_composite_rgb_passthrough():
    region = np.zeros((3, 4, 4), np.uint8)
    region[0] = 200
    out = composite(region, [0, 1, 2], [(0, 255)] * 3, [(255, 0, 0)] * 3, rgb=True)
    assert out.shape == (4, 4, 3)
    assert out[0, 0, 0] == 200 and out[0, 0, 1] == 0


def test_composite_window_and_color():
    region = np.array([[[100, 200]]], np.uint16)  # (1, 1, 2)
    out = composite(region, [0], [(100, 200)], [(0, 255, 0)], rgb=False)
    assert tuple(out[0, 0]) == (0, 0, 0)
    assert tuple(out[0, 1]) == (0, 255, 0)


def test_group_attrs_shape():
    src = PlaneSource(
        data=None,
        dtype=np.dtype("uint8"),
        shape=(3, 1000, 2000),
        rgb=True,
        channels=[ChannelInfo("R", (255, 0, 0)), ChannelInfo("G", (0, 255, 0)),
                  ChannelInfo("B", (0, 0, 255))],
        pixel_size_um=(0.5, 0.5),
        source_name="x.nd2",
        selection={"t": 0, "p": 0, "z": 0},
    )
    shapes = level_shapes(1000, 2000, 512)
    attrs = build_group_attrs(src, shapes, 512, [dict(start=0, end=255, min=0, max=255)] * 3)
    ms = attrs["multiscales"][0]
    assert [a["name"] for a in ms["axes"]] == ["c", "y", "x"]
    assert len(ms["datasets"]) == len(shapes)
    assert ms["datasets"][1]["coordinateTransformations"][0]["scale"] == [1.0, 1.0, 1.0]
    assert attrs["nd2wsi"]["levels"][0]["width"] == 2000


def test_svs_aperio_meta_parsing():
    from nd2wsi.svs import _aperio_meta, is_svs

    desc = "Aperio Image Library v11.2.1\n46920x33014 ... |AppMag = 20|MPP = 0.4990|Filtered=5"
    meta = _aperio_meta(desc)
    assert meta["mpp"] == 0.4990 and meta["magnification"] == 20.0
    assert _aperio_meta("no metadata here") == {}
    assert is_svs("a/b/slide.SVS") and not is_svs("x.nd2")


def test_default_store_path_collects_caches_in_one_folder(tmp_path):
    from nd2wsi.convert import default_store_path

    slide = tmp_path / "23-12089.nd2"
    slide.write_bytes(b"")
    assert default_store_path(slide) == tmp_path / "pyramids" / "23-12089.nd2.ome.zarr"

    legacy = tmp_path / "pyramid_23-12089.ome.zarr"
    legacy.mkdir()
    assert default_store_path(slide) == legacy  # nothing has to be rebuilt

    legacy.rmdir()
    peer = tmp_path / "23-12089.svs"
    peer.write_bytes(b"svs")
    assert default_store_path(slide) != default_store_path(peer)


def test_tidy_caches_moves_only_our_stores(tmp_path):
    from nd2wsi.convert import tidy_caches

    ours = tmp_path / "pyramid_a.ome.zarr"
    ours.mkdir()
    (ours / ".zattrs").write_text('{"nd2wsi": {}, "multiscales": []}')
    older = tmp_path / "b.ome.zarr"
    older.mkdir()
    (older / ".zattrs").write_text('{"nd2wsi": {}, "multiscales": []}')
    foreign = tmp_path / "someone_else.ome.zarr"
    foreign.mkdir()
    (foreign / ".zattrs").write_text('{"multiscales": []}')

    planned = tidy_caches(tmp_path, dry_run=True)["moved"]
    assert {p[0].name for p in planned} == {"pyramid_a.ome.zarr", "b.ome.zarr"}
    assert ours.exists()  # dry run touched nothing

    tidy_caches(tmp_path)
    assert (tmp_path / "pyramids" / "a.ome.zarr").is_dir()
    assert (tmp_path / "pyramids" / "b.ome.zarr").is_dir()
    assert foreign.is_dir() and not ours.exists()


def test_tidy_remove_corrupt_caches_rescues_annotations_first(tmp_path, capsys):
    # plate.py and convert.py quarantine old stores whole because an older
    # build may have put annotations inside; tidy must not destroy them.
    from nd2wsi.cli import main

    caches = tmp_path / "nd2wsi" / "caches"
    plate = caches / "x--plate.nd2svs.corrupt-20261001T000000"
    (plate / "nested").mkdir(parents=True)
    (plate / "nested" / "annotations_slide.nd2.json").write_bytes(b'{"items": [1]}')
    (plate / "chunk.bin").write_bytes(b"\0" * 64)
    pyramid = tmp_path / "pyramids" / "x.ome.zarr.corrupt-20261001T000000"
    pyramid.mkdir(parents=True)
    (pyramid / "annotations_x.nd2.json").write_bytes(b'{"items": [2]}')

    assert main(["tidy", str(tmp_path), "--remove-corrupt-caches"]) == 0
    assert not plate.exists() and not pyramid.exists()
    home = tmp_path / "nd2wsi" / "annotations"
    assert (home / "annotations_slide.nd2.json").read_bytes() == b'{"items": [1]}'
    assert (home / "annotations_x.nd2.json").read_bytes() == b'{"items": [2]}'
    assert "rescued annotations_slide.nd2.json" in capsys.readouterr().out


def test_tidy_keeps_a_quarantined_store_whose_annotations_cannot_be_saved(tmp_path, capsys):
    from nd2wsi.cli import main

    corpse = tmp_path / "nd2wsi" / "caches" / "x.nd2svs.corrupt-1"
    corpse.mkdir(parents=True)
    (corpse / "annotations_x.nd2.json").write_bytes(b"[]")
    # the rescue destination is a file, so nothing can be copied out
    (tmp_path / "nd2wsi" / "annotations").write_bytes(b"")

    assert main(["tidy", str(tmp_path), "--remove-corrupt-caches"]) == 0
    assert (corpse / "annotations_x.nd2.json").read_bytes() == b"[]"
    assert "kept x.nd2svs.corrupt-1" in capsys.readouterr().err


def test_annotations_live_in_the_managed_folder(tmp_path):
    """Work goes to nd2wsi/annotations/, whatever holds the cache, and old
    sidecars migrate there on open."""
    from nd2wsi.server import annotations_sidecar

    attrs = {"nd2wsi": {"source": "24-962.nd2"}}
    home = tmp_path / "nd2wsi" / "annotations" / "annotations_24-962.nd2.json"

    # container-era store
    store = tmp_path / "nd2wsi" / "caches" / "24-962--t0-p0-zmid.nd2wsi-cache" / "store.ome.zarr"
    store.mkdir(parents=True)
    assert annotations_sidecar(store, attrs) == home

    # legacy pyramids store, with a stray sidecar beside the slide
    legacy = tmp_path / "pyramids" / "24-962.ome.zarr"
    legacy.mkdir(parents=True)
    stray = tmp_path / "annotations_24-962.json"
    stray.write_text("[]")
    got = annotations_sidecar(legacy, attrs)
    assert got == home and got.read_text() == "[]"
    assert not stray.exists()


def test_ambiguous_stem_only_annotations_are_not_moved(tmp_path):
    from nd2wsi.server import annotations_sidecar

    (tmp_path / "sample.nd2").write_bytes(b"nd2")
    (tmp_path / "sample.svs").write_bytes(b"svs")
    old = tmp_path / "annotations_sample.json"
    old.write_text('{"items": []}')

    nd2_attrs = {"nd2wsi": {"source": "sample.nd2", "notes": []}}
    svs_attrs = {"nd2wsi": {"source": "sample.svs", "notes": []}}
    nd2_path = annotations_sidecar(tmp_path / "sample.nd2", nd2_attrs)
    svs_path = annotations_sidecar(tmp_path / "sample.svs", svs_attrs)

    assert nd2_path != svs_path
    assert old.exists()
    assert not nd2_path.exists() and not svs_path.exists()
    assert any("ambiguous" in note for note in nd2_attrs["nd2wsi"]["notes"])


def test_provenanced_legacy_annotation_moves_to_the_right_source(tmp_path):
    from nd2wsi.server import annotations_sidecar

    (tmp_path / "sample.nd2").write_bytes(b"nd2")
    (tmp_path / "sample.svs").write_bytes(b"svs")
    old = tmp_path / "annotations_sample.json"
    old.write_text('{"source": {"name": "sample.nd2"}, "items": []}')
    attrs = {"nd2wsi": {"source": "sample.nd2"}}

    target = annotations_sidecar(tmp_path / "sample.nd2", attrs)

    assert target.name == "annotations_sample.nd2.json"
    assert target.exists() and not old.exists()


def test_sweep_appledouble_removes_only_the_twins(tmp_path):
    from nd2wsi.convert import sweep_appledouble

    store = tmp_path / "a.ome.zarr"
    (store / "0").mkdir(parents=True)
    (store / ".zattrs").write_text("{}")
    (store / "0" / "0.0.0").write_bytes(b"chunk")
    (store / "0" / "._0.0.0").write_bytes(b"junk")
    (store / "._.zattrs").write_bytes(b"junk")

    n, freed = sweep_appledouble(store)
    assert n == 2 and freed > 0
    assert (store / "0" / "0.0.0").read_bytes() == b"chunk"
    assert (store / ".zattrs").exists()
    assert not (store / "0" / "._0.0.0").exists()
    assert sweep_appledouble(store) == (0, 0)


def test_rescue_annotations_lifts_work_out_of_a_doomed_folder(tmp_path):
    from nd2wsi.server import rescue_annotations

    store = tmp_path / "pyramids" / "a.ome.zarr"
    store.mkdir(parents=True)
    (store / "annotations_a.json").write_text('{"items": []}')
    (store / "0.0.0").write_bytes(b"chunk")
    (tmp_path / "annotations_b.json").write_text("keep me")
    (store.parent / "annotations_b.json").write_text("newer duplicate")

    saved = rescue_annotations(store.parent, tmp_path)
    assert (tmp_path / "annotations_a.json").exists()
    assert len(saved) == 2
    assert any(p.name == "annotations_a.json" for p in saved)
    # an existing file beside the slide is never overwritten
    assert (tmp_path / "annotations_b.json").read_text() == "keep me"
    rescued = [p for p in saved if p.name.startswith("annotations_b.rescued-")]
    assert len(rescued) == 1
    assert rescued[0].read_text() == "newer duplicate"


def test_concurrent_annotation_rescues_preserve_both_colliding_files(
    tmp_path, monkeypatch
):
    from nd2wsi.server import rescue_annotations

    first = tmp_path / "first"
    second = tmp_path / "second"
    home = tmp_path / "safe"
    first.mkdir()
    second.mkdir()
    home.mkdir()
    name = "annotations_same.json"
    (first / name).write_text("first")
    (second / name).write_text("second")

    barrier = threading.Barrier(2)
    real_open = os.open

    def synchronized_open(path, flags, mode=0o777):
        if Path(path) == home / name and flags & os.O_EXCL:
            barrier.wait(timeout=5)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", synchronized_open)
    errors = []

    def rescue(folder):
        try:
            rescue_annotations(folder, home)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=rescue, args=(folder,)) for folder in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert {path.read_text() for path in home.glob("annotations_same*.json")} == {
        "first",
        "second",
    }


def test_rescue_never_accepts_a_symlink_back_into_the_doomed_cache(tmp_path, symlink_support):
    from nd2wsi.server import rescue_annotations

    folder = tmp_path / "doomed"
    home = tmp_path / "safe"
    folder.mkdir()
    home.mkdir()
    source = folder / "annotations_precious.json"
    source.write_text('{"items":[1]}')
    unsafe = home / source.name
    unsafe.symlink_to(source)

    saved = rescue_annotations(folder, home)

    assert len(saved) == 1
    assert saved[0] != unsafe and not saved[0].is_symlink()
    assert saved[0].read_text() == source.read_text()
    source.unlink()
    assert saved[0].read_text() == '{"items":[1]}'
    assert not unsafe.exists()  # the rejected symlink is now dangling


def test_rescue_flushes_the_file_and_supported_destination_directory(tmp_path, monkeypatch):
    from nd2wsi.server import rescue_annotations

    folder = tmp_path / "doomed"
    home = tmp_path / "safe"
    folder.mkdir()
    (folder / "annotations_a.json").write_text('{"items":[]}')
    real_fsync = os.fsync
    directory_syncs = []
    file_syncs = []

    def record_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs.append(fd)
        else:
            file_syncs.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record_fsync)
    saved = rescue_annotations(folder, home)

    assert len(saved) == 1 and saved[0].read_text() == '{"items":[]}'
    assert file_syncs
    if os.name != "nt":
        assert directory_syncs


def test_auto_tile_follows_the_volume(monkeypatch, tmp_path):
    from nd2wsi import convert as c

    monkeypatch.setattr(c, "_block_bytes", lambda p: 4096)
    assert c.auto_tile(tmp_path / "x.ome.zarr") == 512
    monkeypatch.setattr(c, "_block_bytes", lambda p: 1048576)
    assert c.auto_tile(tmp_path / "x.ome.zarr") == 1024
    # the store's directories may not exist yet, only the volume does
    monkeypatch.setattr(c, "_block_bytes", lambda p: 131072)
    assert c.auto_tile(tmp_path / "not" / "yet" / "x.ome.zarr") == 1024


def test_explicit_tile_still_wins(monkeypatch, tmp_path):
    import zarr

    from nd2wsi import convert as c

    pytest.importorskip("tifffile")
    pytest.importorskip("imagecodecs")
    import numpy as np
    import tifffile

    img = np.random.default_rng(0).integers(0, 255, (700, 900, 3), np.uint8)
    slide = tmp_path / "s.svs"
    tifffile.imwrite(slide, img, tile=(240, 240), photometric="rgb",
                     compression="jpeg2000", compressionargs={"reversible": True})
    monkeypatch.setattr(c, "_block_bytes", lambda p: 1048576)  # would pick 1024
    out = tmp_path / "s.ome.zarr"
    c.convert(slide, out, tile=256, progress=False)
    root = zarr.open_group(str(out), mode="r")
    assert root.attrs["nd2wsi"]["tile"] == 256
    assert root["0"].chunks == (1, 256, 256)


def test_downsample_batches_are_width_independent():
    """A task's memory comes from the tile and a fixed budget, never the
    slide width — the old full-width stripes reached gigabytes."""
    from nd2wsi.convert import DOWNSAMPLE_TASK_BUDGET, _batch_cols

    for nt, itemsize in [(512, 1), (512, 2), (1024, 2), (1024, 4)]:
        bw = _batch_cols(nt, itemsize)
        per_col = 2 * nt * 2 * itemsize + nt * 4 + nt * itemsize
        assert bw % nt == 0 and bw >= nt
        assert bw * per_col <= DOWNSAMPLE_TASK_BUDGET * 1.05


def test_annotation_sidecars_follow_source_and_plane(tmp_path):
    from nd2wsi.server import annotations_sidecar

    store = tmp_path / "store.ome.zarr"
    store.mkdir()

    def attrs(source, z):
        return {
            "nd2wsi": {
                "source": source,
                "selection": {"t": 0, "p": 0, "z": z},
            }
        }

    z0 = annotations_sidecar(store, attrs("slide.nd2", 0))
    z1 = annotations_sidecar(store, attrs("slide.nd2", 1))
    svs = annotations_sidecar(store, attrs("slide.svs", 0))
    assert len({z0, z1, svs}) == 3
    assert "slide.nd2" in z0.name and "z0" in z0.name
    assert "z1" in z1.name
    assert "slide.svs" in svs.name


@pytest.mark.parametrize("z_resolved", [
    "x/../../../../victim/planted", "..\\..\\victim", "mid/..", -1, True, 1.5, [0],
])
def test_cache_manifest_selection_cannot_steer_the_sidecar_path(tmp_path, z_resolved):
    # A .nd2svs from someone else carries its own manifest; its plane
    # values must not decide where annotation files are written.
    from nd2wsi.annotation_sidecars import annotations_sidecar
    from nd2wsi.storage.single_file import write_entry

    received = tmp_path / "received"
    received.mkdir()
    cache = received / "slide--t0-p0-zmid.nd2svs"
    write_entry(cache, "manifest.json", json.dumps({
        "format": "nd2wsi-cache/5", "kind": "full", "complete": True,
        "source": {"name": "slide.nd2"},
        "selection": {"t": 0, "p": 0, "z": "x", "z_resolved": z_resolved},
    }).encode())
    (received / "annotations_slide.json").write_text("[]")
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    with pytest.raises(ValueError):
        annotations_sidecar(cache, {"nd2wsi": {"source": "slide.nd2"}})
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


def test_a_cache_from_elsewhere_cannot_move_the_annotations_folder(tmp_path):
    # Its manifest may name any existing same-named file as its source; only
    # the file it was really built from (by fingerprint) decides the folder.
    from nd2wsi.annotation_sidecars import annotations_sidecar
    from nd2wsi.cache import quick_fingerprint
    from nd2wsi.storage.single_file import write_entry

    received, other = tmp_path / "received", tmp_path / "other"
    received.mkdir()
    other.mkdir()
    (other / "slide.nd2").write_bytes(b"unrelated")
    cache = received / "slide--t0-p0-zmid.nd2svs"

    def manifest(**source):
        write_entry(cache, "manifest.json", json.dumps({
            "format": "nd2wsi-cache/5", "kind": "full", "complete": True,
            "source": {"name": "slide.nd2", "relative_path": "../other/slide.nd2", **source},
            "selection": {"t": 0, "p": 0, "z": "mid", "z_resolved": 0},
        }).encode())

    manifest()
    (received / "annotations_slide.json").write_text("[]")
    path = annotations_sidecar(cache, {"nd2wsi": {"source": "slide.nd2"}})
    assert path.parent == received / "nd2wsi" / "annotations"
    assert sorted(p.name for p in other.iterdir()) == ["slide.nd2"]

    # nor can a matching fingerprint, which a copy that keeps mtimes makes
    # easy to know: every real cache keeps its source in this folder anyway
    manifest(**quick_fingerprint(other / "slide.nd2"))
    path = annotations_sidecar(cache, {"nd2wsi": {"source": "slide.nd2"}}, migrate=False)
    assert path.parent == received / "nd2wsi" / "annotations"
    assert sorted(p.name for p in other.iterdir()) == ["slide.nd2"]


@pytest.mark.parametrize("layout, rel", [
    ("nd2wsi/caches/slide--t0-p0-zmid.nd2svs", "../../slide.nd2"),
    ("nd2wsi/caches/slide--t0-p0-zmid.nd2wsi-cache", "../../../slide.nd2"),
    ("slide--t0-p0-zmid.nd2svs", "slide.nd2"),
])
def test_manifest_source_is_accepted_only_in_the_slides_folder(tmp_path, layout, rel):
    from nd2wsi.annotation_sidecars import _manifest_source_path

    slide = tmp_path / "slides" / "slide.nd2"
    slide.parent.mkdir()
    slide.write_bytes(b"nd2")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "slide.nd2").write_bytes(b"someone else's")
    container = slide.parent / layout
    container.parent.mkdir(parents=True, exist_ok=True)
    found = _manifest_source_path(container, {"source": {"name": "slide.nd2", "relative_path": rel}})
    assert found == slide.resolve()
    for outside in ("../" * rel.count("../") + "../other/slide.nd2", "../" * 6, str(tmp_path / "other" / "slide.nd2")):
        assert _manifest_source_path(container, {"source": {"relative_path": outside}}) is None
    # a directory in the slide's folder is never a source; a deleted source
    # keeps its path as provenance
    (slide.parent / "subdir").mkdir()
    assert _manifest_source_path(container, {"source": {"relative_path": rel.replace("slide.nd2", "subdir")}}) is None
    slide.unlink()
    assert _manifest_source_path(container, {"source": {"name": "slide.nd2", "relative_path": rel}}) == slide.resolve()


@pytest.mark.parametrize("selection", [["t"], 1, "t0"])
def test_a_cache_with_an_unreadable_plane_selection_is_refused(tmp_path, selection):
    from nd2wsi.annotation_sidecars import annotations_sidecar
    from nd2wsi.storage.single_file import write_entry

    cache = tmp_path / "slide--t0-p0-zmid.nd2svs"
    write_entry(cache, "manifest.json", json.dumps({
        "format": "nd2wsi-cache/5", "kind": "full", "complete": True,
        "source": {"name": "slide.nd2"}, "selection": selection,
    }).encode())
    with pytest.raises(ValueError):
        annotations_sidecar(cache, {"nd2wsi": {"source": "slide.nd2"}})


def test_selection_tag_accepts_the_planes_the_app_writes():
    from nd2wsi.cache import selection_tag
    from nd2wsi.reader import PlaneSelection

    assert selection_tag(PlaneSelection()) == "t0-p0-zmid"
    assert selection_tag(PlaneSelection(t=2, p=3, z="max")) == "t2-p3-zmax"
    assert selection_tag({"t": "1", "p": 0, "z": 4}) == "t1-p0-z4"
    # an SVS cache records z_resolved None; existing sidecars carry that name
    assert selection_tag({"t": 0, "p": 0, "z": None}) == "t0-p0-zNone"
    assert selection_tag({"t": np.int64(2), "p": 0, "z": np.uint16(3)}) == "t2-p0-z3"
    for bad in ({"t": "../a"}, {"p": -2}, {"z": "mid/.."}, {"t": False}):
        with pytest.raises(ValueError):
            selection_tag(bad)


def test_auto_workers_respects_cpu_memory_and_absolute_caps(monkeypatch):
    from nd2wsi import convert as c

    monkeypatch.setattr(c.os, "cpu_count", lambda: 192)
    monkeypatch.setattr(c, "available_memory_bytes", lambda: 64 * 1024**3)
    assert c.auto_workers() == c.MAX_AUTO_WORKERS

    monkeypatch.setattr(c, "available_memory_bytes", lambda: 192 * 1024**2)
    assert c.auto_workers() == 2  # floor: two workers, never one

    monkeypatch.setattr(c.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(c, "available_memory_bytes", lambda: 64 * 1024**3)
    assert c.auto_workers() == 7

    # where the platform cannot say (macOS), only the CPU rule applies
    monkeypatch.setattr(c, "available_memory_bytes", lambda: None)
    monkeypatch.setattr(c.os, "cpu_count", lambda: 12)
    assert c.auto_workers() == 10


def test_windows_worker_and_tile_policies_use_os_memory_and_cluster_size(monkeypatch):
    from types import SimpleNamespace

    from nd2wsi import convert as c

    monkeypatch.setattr(c, "os", SimpleNamespace(name="nt", cpu_count=lambda: 32))
    monkeypatch.setattr(c, "windows_available_memory_bytes", lambda: 192 * 1024**2)
    monkeypatch.setattr(c, "windows_allocation_block_bytes", lambda _path: 1024**2)
    assert c.auto_workers() == 2
    assert c._block_bytes("X:\\microscopy") == 1024**2


# -- title-bar double-click: the tab strip stands in for the title bar


def _fake_foundation(monkeypatch, value):
    import sys
    import types

    class _Defaults:
        def stringForKey_(self, key):
            assert key == "AppleActionOnDoubleClick"
            return value

    class _NSUserDefaults:
        @staticmethod
        def standardUserDefaults():
            return _Defaults()

    monkeypatch.setitem(sys.modules, "Foundation", types.SimpleNamespace(NSUserDefaults=_NSUserDefaults))


@pytest.mark.parametrize(
    "stored, action",
    [
        (None, "zoom"),  # unset: the macOS default
        ("Maximize", "zoom"),
        ("Fill", "zoom"),  # macOS 15+ offers Fill; zoom is the closest public action
        ("Minimize", "minimize"),
        ("None", "none"),
    ],
)
def test_title_bar_double_click_follows_the_system_setting(monkeypatch, stored, action):
    from nd2wsi import app

    _fake_foundation(monkeypatch, stored)
    assert app.title_bar_double_click_action() == action


def test_title_bar_double_click_zooms_when_the_setting_cannot_be_read(monkeypatch):
    import sys
    import types

    from nd2wsi import app

    class _Broken:
        @staticmethod
        def standardUserDefaults():
            raise RuntimeError("no defaults here")

    monkeypatch.setitem(sys.modules, "Foundation", types.SimpleNamespace(NSUserDefaults=_Broken))
    assert app.title_bar_double_click_action() == "zoom"
    monkeypatch.setitem(sys.modules, "Foundation", None)  # import fails outright, as on Linux CI
    assert app.title_bar_double_click_action() == "zoom"
