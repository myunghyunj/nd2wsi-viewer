"""Scientific metadata and annotation regressions found in the 2.6.1 review."""

import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest
import tifffile
from http_fixture import serving
from test_czi import build_synthetic_czi
from test_lif import build_synthetic_lif

from nd2wsi.annotation_sidecars import annotations_sidecar, plate_annotations_sidecar
from nd2wsi.convert import ensure_cache, open_store
from nd2wsi.plate import PlateSource, is_plate_file
from nd2wsi.reader import PlaneSelection
from nd2wsi.server import create_server, server_url
from nd2wsi.window_sessions import create_window_session


def lif_series(value=40, *, um=1, lut="Green", tiles=None, nt=1, nz=1, magnification=20):
    h = w = 32
    nm = len(tiles) if tiles else 1
    dims = [
        f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="1" Length="{(w - 1) * um * 1e-6}" Unit="m"/>',
        f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w}" Length="{(h - 1) * um * 1e-6}" Unit="m"/>',
    ]
    if tiles:
        dims.append(f'<DimensionDescription DimID="10" NumberOfElements="{nm}" BytesInc="{w*h}"/>')
    if nz > 1:
        dims.append(f'<DimensionDescription DimID="3" NumberOfElements="{nz}" BytesInc="{w*h*nm}" Length="0.000004" Unit="m"/>')
    if nt > 1:
        dims.append(f'<DimensionDescription DimID="4" NumberOfElements="{nt}" BytesInc="{w*h*nm*nz}" Length="120" Unit="s"/>')
    extra = f'<ATLConfocalSettingDefinition Magnification="{magnification}"/>'
    if tiles:
        extra += '<Attachment Name="TileScanInfo">' + ''.join(
            f'<Tile FieldX="{x}" FieldY="{y}"/>' for x, y in tiles
        ) + '</Attachment>'
    return {
        "name": f"Series_{value}",
        "payload": np.full((nt, nz, nm, h, w), value, np.uint8).tobytes(),
        "dims_xml": dims,
        "chans_xml": [f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="{lut}" BytesInc="0"/>'],
        "extra_xml": extra,
    }


def request(base, route, data=None, *, raw=False):
    req = urllib.request.Request(
        base + route, data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.read() if raw else json.load(response)


@pytest.fixture(params=["lif", "czi"])
def confocal_path(tmp_path, request):
    if request.param == "lif":
        return build_synthetic_lif(tmp_path / "stack.lif", [lif_series(nt=2, nz=2)])
    return build_synthetic_czi(tmp_path / "stack.czi", {
        (t, z, 0, 0): np.full((32, 32), 40 + t * 10 + z, np.uint16)
        for t in range(2) for z in range(2)
    })


@pytest.mark.parametrize("difference", ["calibration", "channels", "magnification", "mosaic"])
def test_incompatible_lif_series_open_separately_with_correct_export_metadata(tmp_path, difference):
    options = {
        "calibration": {"um": 2}, "channels": {"lut": "Red"},
        "magnification": {"magnification": 40}, "mosaic": {"tiles": [(0, 0), (0, 1)]},
    }[difference]
    first_options = {"tiles": [(0, 0), (1, 0)]} if difference == "mosaic" else {}
    path = build_synthetic_lif(tmp_path / "mixed.lif", [
        lif_series(**first_options), lif_series(180, **options), lif_series(90, **first_options),
    ])
    # Do not silently select a compatible subset and renumber later sites.
    assert not is_plate_file(path)
    with pytest.raises(ValueError, match="different geometry, calibration or channels"):
        PlateSource(path, store=False)
    # The ordinary source open and explicit series conversion both remain usable.
    with serving(create_server([], port=0)) as httpd:
        base = server_url(httpd)
        first = request(base, "api/open", {"path": str(path)})["sid"]
        assert request(base, f"s/{first}/api/info")["kind"] == "slide"
        selected = ensure_cache(path, selection=PlaneSelection(p=1, z=0), tile=32)
        sid = request(base, "api/open", {"path": str(selected)})["sid"]
        info = request(base, f"s/{sid}/api/info")
        expected_um = options.get("um", 1)
        assert info["pixelSizeUm"] == [expected_um, expected_um]
        assert info["width"] == 32
        assert info["height"] == (64 if difference == "mosaic" else 32)
        assert info["selection"]["p"] == 1
        assert info["channels"][0]["color"] == ("FF0000" if difference == "channels" else "00FF00")
        data = request(base, f"s/{sid}/api/roi?format=tiff&x=0&y=0&w=32&h={info['height']}", raw=True)
        with tifffile.TiffFile(io.BytesIO(data)) as tif:
            assert np.all(tif.asarray() == 180)
            num, den = tif.pages[0].tags["XResolution"].value
            assert num / den == pytest.approx(1e4 / expected_um)


def test_compatible_lif_series_keep_site_indices_and_pixels(tmp_path):
    path = build_synthetic_lif(tmp_path / "matching.lif", [lif_series(40), lif_series(180)])
    assert is_plate_file(path)
    source = PlateSource(path, store=False)
    try:
        assert source.P == 2
        assert np.all(source.frame(0, 0, 0) == 40)
        assert np.all(source.frame(0, 1, 0) == 180)
    finally:
        source.close()


def test_confocal_timing_is_unknown_not_ordinal_milliseconds(confocal_path):
    source = PlateSource(confocal_path, store=False)
    try:
        assert source.T == source.Z == 2
        assert source.attrs["nd2wsi"]["plate"]["timesMs"] == []
        assert source.attrs["nd2wsi"]["plate"]["periodMs"] is None
    finally:
        source.close()


def test_old_plane_annotations_are_visible_and_t_z_edits_are_isolated(confocal_path):
    # This is the pre-2.6.1 conversion/sidecar path, not a manually guessed name.
    cache = ensure_cache(confocal_path, selection=PlaneSelection(t=0, p=0, z=1), tile=32)
    root, attrs = open_store(cache)
    try:
        old = annotations_sidecar(cache, attrs)
        original = b'{"items":[{"id":"manual","type":"pin","x":1.25,"y":2.5}],"extension":"keep"}'
        old.write_bytes(original)
    finally:
        root.store.close()
    with serving(create_server([], port=0)) as httpd:
        base = server_url(httpd)
        sid = request(base, "api/open", {"path": str(confocal_path)})["sid"]
        assert request(base, f"s/{sid}/api/info")["plate"]["annotationScope"] == "plane"
        route = f"s/{sid}/api/annotations?p=0"
        home = request(base, route + "&t=0&z=1")
        assert Path(home["path"]) == old and home["items"][0]["id"] == "manual"
        assert request(base, route + "&t=0&z=0")["items"] == []
        request(base, route + "&t=0&z=0", {"items": [{"id": "z0"}]})
        assert request(base, route + "&t=1&z=0")["items"] == []
        request(base, route + "&t=1&z=0", {"items": []})
        assert request(base, route + "&t=0&z=0")["items"] == [{"id": "z0"}]
        assert old.read_bytes() == original
        saved = Path(request(base, route + "&t=0&z=0")["path"])
        assert json.loads(saved.read_bytes())["selection"] == {"t": 0, "p": 0, "z": 0}
        for bad in ("&t=-1&z=0", "&t=0&z=2", "&t=oops&z=0"):
            with pytest.raises(urllib.error.HTTPError) as error:
                request(base, route + bad, {"items": []})
            assert error.value.code == 400
        assert old.read_bytes() == original


@pytest.mark.parametrize("role", [None, "user", "agent"])
def test_shared_261_sidecars_are_preserved_without_merging(confocal_path, tmp_path, role):
    attrs = {"nd2wsi": {"source": str(confocal_path)}}
    shared = plate_annotations_sidecar(confocal_path, attrs, 0)
    original = b'{ "items": [{"id":"shared"}], "custom": {"manual":true} }\n'
    shared.write_bytes(original)
    before = sorted(p.relative_to(confocal_path.parent) for p in confocal_path.parent.rglob("*"))
    session = create_window_session(role, tmp_path / "sessions") if role else None
    with serving(create_server([], port=0, window_session=session)) as httpd:
        base = server_url(httpd)
        sid = request(base, "api/open", {"path": str(confocal_path)})["sid"]
        route = f"s/{sid}/api/annotations?p=0&t=0"
        a, b = request(base, route + "&z=0"), request(base, route + "&z=1")
        assert a["path"] != b["path"]
        assert Path(a["path"]).read_bytes() == original
        assert Path(b["path"]).read_bytes() == original
        request(base, route + "&z=0", {"items": [], **({"expected_revision": a["revision"]} if session else {})})
        assert request(base, route + "&z=0")["items"] == []
        assert request(base, route + "&z=1")["items"] == [{"id": "shared"}]
        assert shared.read_bytes() == original
    if role == "agent":
        # Agent session artifacts are private; source-side trees are untouched.
        after = sorted(p.relative_to(confocal_path.parent) for p in confocal_path.parent.rglob("*")
                       if not p.is_relative_to(tmp_path / "sessions"))
        assert after == before
    if session:
        session.mark_closed()


def test_a4_uint16_rgb_plate_windows_and_persisted_261_manifest_recovery(tmp_path):
    from PIL import Image

    from nd2wsi.cache import read_manifest
    from nd2wsi.plate import WINDOWS_KEY
    from nd2wsi.storage.single_file import SQLiteStore

    rgb = np.broadcast_to(np.array([1000, 10000, 30000], np.uint16), (32, 32, 3)).copy()
    czi_path = build_synthetic_czi(tmp_path / "rgb16.czi", {(z, 0, 0): rgb for z in range(2)}, pixel_type=4)

    source = PlateSource(czi_path)
    try:
        cache_path = source.store.container
        for ch in source.attrs["omero"]["channels"]:
            assert ch["window"]["start"] == 0.0
            assert ch["window"]["end"] == 65535.0
            assert ch["window"]["max"] == 65535.0
        im = Image.open(io.BytesIO(source.render_frame(0, 0, 0, 8, [0, 1, 2], fmt="png")))
        assert list(im.getpixel((0, 0))) == [3, 38, 116]
    finally:
        source.close()

    # Forge a defective 2.6.1 persisted window record ([0, 255]) in the .nd2svs manifest
    manifest = read_manifest(cache_path)
    for win in manifest[WINDOWS_KEY]["windows"]:
        win["end"] = 255.0
    with SQLiteStore(cache_path) as db:
        db.write_bytes("manifest.json", json.dumps(manifest).encode("utf-8"))

    reopened = PlateSource(czi_path, cache_path=cache_path)
    try:
        for ch in reopened.attrs["omero"]["channels"]:
            assert ch["window"]["end"] == 65535.0
        im2 = Image.open(io.BytesIO(reopened.render_frame(0, 0, 0, 8, [0, 1, 2], fmt="png")))
        assert list(im2.getpixel((0, 0))) == [3, 38, 116]
        updated = read_manifest(cache_path)
        assert all(w["end"] == 65535.0 for w in updated[WINDOWS_KEY]["windows"])
    finally:
        reopened.close()


def test_a6_mixed_bit_depth_lif_channels_and_unsupported_layout_rejection(tmp_path):
    from contextlib import ExitStack

    from nd2wsi.lif import open_lif

    raw = np.tile(np.array([17, 232, 3], np.uint8), 32 * 32 * 2)
    mixed_path = build_synthetic_lif(
        tmp_path / "mixed.lif",
        [
            {
                "payload": raw.tobytes(),
                "dims_xml": [
                    '<DimensionDescription DimID="1" NumberOfElements="32" BytesInc="3" Length="0.000032" Unit="m"/>',
                    '<DimensionDescription DimID="2" NumberOfElements="32" BytesInc="96" Length="0.000032" Unit="m"/>',
                    '<DimensionDescription DimID="3" NumberOfElements="2" BytesInc="3072" Length="0.000004" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" Resolution="8" LUTName="Green" BytesInc="0"/>',
                    '<ChannelDescription DataType="0" Resolution="16" LUTName="Red" BytesInc="1"/>',
                ],
            }
        ],
    )
    with ExitStack() as stack:
        plane = open_lif(stack, mixed_path)
        arr = plane.data.compute()
        assert arr.dtype == np.dtype("uint16")
        assert arr[:, 0, 0].tolist() == [17, 1000]

    plate = PlateSource(mixed_path, store=False)
    try:
        frame = plate.frame(0, 0, 0)
        assert frame.dtype == np.dtype("uint16")
        assert frame[:, 0, 0].tolist() == [17, 1000]
    finally:
        plate.close()

    # >16-bit integer channels must be explicitly rejected, not converted to float32
    u32_path = build_synthetic_lif(
        tmp_path / "u32.lif",
        [
            {
                "payload": np.full((32, 32), 16777217, dtype=np.uint32).tobytes(),
                "dims_xml": [
                    '<DimensionDescription DimID="1" NumberOfElements="32" BytesInc="4" Length="0.000032" Unit="m"/>',
                    '<DimensionDescription DimID="2" NumberOfElements="32" BytesInc="128" Length="0.000032" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" Resolution="32" BytesInc="0"/>',
                ],
            }
        ],
    )
    with ExitStack() as stack, pytest.raises(ValueError, match="unsupported LIF integer channel resolution 32"):
        open_lif(stack, u32_path)

    # Overlapping pixel-interleaved channel offsets must be rejected
    overlap_path = build_synthetic_lif(
        tmp_path / "overlap.lif",
        [
            {
                "payload": bytes(32 * 32 * 2),
                "dims_xml": [
                    '<DimensionDescription DimID="1" NumberOfElements="32" BytesInc="2" Length="0.000032" Unit="m"/>',
                    '<DimensionDescription DimID="2" NumberOfElements="32" BytesInc="64" Length="0.000032" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" Resolution="16" BytesInc="0"/>',
                    '<ChannelDescription DataType="0" Resolution="8" BytesInc="1"/>',
                ],
            }
        ],
    )
    with ExitStack() as stack, pytest.raises(ValueError, match="overlapping channel offsets"):
        open_lif(stack, overlap_path)


def test_a8_czi_channel_metadata_maps_by_subblock_channel_id(tmp_path):
    from contextlib import ExitStack

    from nd2wsi.czi import collect_czi_info, open_czi

    czi_path = build_synthetic_czi(
        tmp_path / "sparse_ch.czi",
        {
            (0, 1, 0): np.full((32, 32), 7, np.uint16),
            (1, 1, 0): np.full((32, 32), 9, np.uint16),
        },
        channels=[("DAPI", "#FF0000FF"), ("EGFP", "#FF00FF00"), ("CY5", "#FFFF0000")],
    )
    with ExitStack() as stack:
        src = open_czi(stack, czi_path)
        assert [ch.name for ch in src.channels] == ["EGFP"]
        assert [ch.color for ch in src.channels] == [(0, 255, 0)]

    info = collect_czi_info(czi_path)
    assert [ch["name"] for ch in info["channel_info"]] == ["EGFP"]

    plate = PlateSource(czi_path, store=False)
    try:
        assert [ch.name for ch in plate.channels] == ["EGFP"]
        assert plate.attrs["omero"]["channels"][0]["label"] == "EGFP"
    finally:
        plate.close()


def test_a9_legacy_cache_discovery_and_cli_explicit_view_selection(tmp_path, monkeypatch, capsys):
    from nd2wsi import cli, server
    from nd2wsi.convert import _legacy_store, convert, default_store_path

    czi_path = build_synthetic_czi(
        tmp_path / "stack3.czi",
        {(z, 0, 0): np.full((32, 32), (z + 1) * 100, np.uint16) for z in range(3)},
    )
    legacy_store = default_store_path(czi_path)
    convert(czi_path, legacy_store, selection=PlaneSelection(z="max"), tile=32, progress=False)
    _, legacy_attrs = open_store(legacy_store)
    sidecar = annotations_sidecar(legacy_store, legacy_attrs)
    sidecar_bytes = b'{"items":[{"id":"max-note","type":"pin","x":4,"y":4}]}\n'
    sidecar.write_bytes(sidecar_bytes)

    # Default middle-plane discovery must reject the Z-max legacy store without
    # quarantining it or touching its sidecar annotations.
    assert _legacy_store(czi_path) is None
    assert _legacy_store(czi_path, PlaneSelection(z="max")) == legacy_store
    default_cache = ensure_cache(czi_path, tile=32)
    assert default_cache != legacy_store
    assert legacy_store.exists()
    assert sidecar.read_bytes() == sidecar_bytes
    root_mid, attrs_mid = open_store(default_cache)
    try:
        assert attrs_mid["nd2wsi"]["selection"]["z"] == 1
        assert int(np.asarray(root_mid["0"][0, 0, 0])) == 200
    finally:
        root_mid.close()

    # CLI explicit view: viewing --z max reuses the matching legacy store,
    # whereas viewing --z 0 builds/opens a z=0 cache without overwriting legacy_store.
    served: list[Path] = []
    monkeypatch.setattr(server, "serve", lambda stores, **kw: served.extend(stores))
    assert cli.main(["view", str(czi_path), "--z", "max", "--yes"]) == 0
    out_max = capsys.readouterr().out
    assert "reusing existing pyramid" in out_max
    assert served[-1] == legacy_store

    served.clear()
    assert cli.main(["view", str(czi_path), "--z", "0", "--yes"]) == 0
    out_z0 = capsys.readouterr().out
    assert "reusing existing pyramid" not in out_z0
    assert served[-1] != legacy_store
    assert legacy_store.exists()
    assert sidecar.read_bytes() == sidecar_bytes
    root_z0, attrs_z0 = open_store(served[-1])
    try:
        assert attrs_z0["nd2wsi"]["selection"]["z"] == 0
        assert int(np.asarray(root_z0["0"][0, 0, 0])) == 100
    finally:
        root_z0.close()

    # Numeric middle-plane legacy store (z=1 of 3) IS accepted as the default mid cache.
    czi_mid_path = build_synthetic_czi(
        tmp_path / "stack3_mid.czi",
        {(z, 0, 0): np.full((32, 32), (z + 1) * 10, np.uint16) for z in range(3)},
    )
    legacy_mid = default_store_path(czi_mid_path)
    convert(czi_mid_path, legacy_mid, selection=PlaneSelection(z="mid"), tile=32, progress=False)
    assert _legacy_store(czi_mid_path) == legacy_mid
    assert ensure_cache(czi_mid_path, tile=32) == legacy_mid


def test_a11_a13_a14_cleanups_kindmark_strip_and_plate_cache_error(tmp_path):
    from node_host import run_node

    from nd2wsi.server import SlideRegistry

    static_dir = Path(__file__).resolve().parent.parent / "nd2wsi" / "static"
    script = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const source = fs.readFileSync(path.join(process.argv[1], 'app.js'), 'utf8').replace(/\r\n/g, '\n');
const k0 = source.indexOf('function kindMark('), k1 = source.indexOf('\n}\n', k0);
const p0 = source.indexOf('function paintPlate('), p1 = source.indexOf('\n}\n', p0);
const cell = () => {
  const img = { dataset: {}, src: '', getAttribute: k => k === 'src' ? img.src : null };
  return { dataset: {}, img, setAttribute(){}, classList: { remove(){}, toggle(){} }, querySelector: () => img };
};
const sites = [{ i: 0, name: 'S1' }];
const state = {
  info: { plate: { sites, P: 1, T: 1, Z: 4 } },
  plate: { t: 0, z: 2, auto: false, focus: 0, k: 8, playing: false, gridEls: [cell()], stripEls: [cell()], focusMap: null }
};
const ctx = vm.createContext({
  state, window: { Nd2PlateUI: { focusReady: () => true } },
  plateGroupKey: () => '0', plateZFor: () => 2, siteLabelText: String,
  plateFrameUrl: (t, p, z, k) => `${t}/${p}/${z}/k${k}`,
  setTimeout: () => 1, clearTimeout(){}
});
vm.runInContext(source.slice(k0, k1) + '\n}\n' + source.slice(p0, p1) + '\n}\n', ctx);
ctx.paintPlate();
const hiddenSrc = state.plate.stripEls[0].img.src;
state.info.plate.T = 3;
ctx.paintPlate();
const timelapseSrc = state.plate.stripEls[0].img.src;
console.log(JSON.stringify({
  lif3d: ctx.kindMark({ notes: ['series has 4 Z planes; converting z=2', 'file has 3 series; converting position 0 only'] }),
  cziMip: ctx.kindMark({ notes: ['Z: maximum-intensity projection over 5 planes', 'file has 2 scenes; converting position 0 only'] }),
  hiddenSrc,
  timelapseSrc,
}));
"""
    res = json.loads(run_node(script, static_dir, check=True).stdout)
    assert res["lif3d"] == "3D PLATE"
    assert res["cziMip"] == "3D PLATE"
    assert res["hiddenSrc"] == ""
    assert res["timelapseSrc"] == "0/0/2/k8"

    czi_path = build_synthetic_czi(
        tmp_path / "plate_err.czi",
        {(z, 0, 0): np.full((32, 32), z + 1, np.uint16) for z in range(2)},
    )
    src = PlateSource(czi_path)
    try:
        cache_file = src.store.container
    finally:
        src.close()
    czi_path.unlink()
    with pytest.raises(ValueError, match="original CZI source"):
        SlideRegistry().open_path(cache_file)


def test_p1_upgrade_rebuilds_old_mixed_lif_and_sparse_czi_caches_preserving_annotations(tmp_path):
    from nd2wsi.annotation_sidecars import annotations_sidecar, confocal_annotations_sidecar
    from nd2wsi.convert import convert, default_store_path, ensure_cache, open_store
    from nd2wsi.storage.single_file import read_entry, write_entry

    # 1. Mixed-depth LIF: simulate a 2.6.1 plate cache and pyramid cache that
    # stored corrupted [59409, 1000] pixels and lacked channel_dtypes.
    w, h = 32, 32
    raw = np.tile(np.array([17, 232, 3], np.uint8), w * h * 2)
    lif_path = build_synthetic_lif(
        tmp_path / "mixed_upgrade.lif",
        [
            {
                "payload": raw.tobytes(),
                "dims_xml": [
                    '<DimensionDescription DimID="1" NumberOfElements="32" BytesInc="3" Length="0.000032" Unit="m"/>',
                    '<DimensionDescription DimID="2" NumberOfElements="32" BytesInc="96" Length="0.000032" Unit="m"/>',
                    '<DimensionDescription DimID="3" NumberOfElements="2" BytesInc="3072" Length="0.000004" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" Resolution="8" LUTName="Green" BytesInc="0"/>',
                    '<ChannelDescription DataType="0" Resolution="16" LUTName="Red" BytesInc="1"/>',
                ],
            }
        ],
    )

    # Build a plate cache and mutate it into the 2.6.1 state (corrupted [59409, 1000]
    # thumbnail, corrupted window, no channel_dtypes, created_by 2.6.1).
    plate_src = PlateSource(lif_path)
    try:
        plate_container = plate_src.store.container
        ann_canon, _ = confocal_annotations_sidecar(lif_path, plate_src.attrs, (0, 0, 0))
        ann_canon.parent.mkdir(parents=True, exist_ok=True)
        ann_bytes = b'{"items":[{"id":"roi-lif-plate"}]}'
        ann_canon.write_bytes(ann_bytes)
        corrupted_frame = np.zeros((2, h, w), dtype=np.uint16)
        corrupted_frame[0] = 59409
        corrupted_frame[1] = 1000
        plate_src.store.put(0, 0, 0, corrupted_frame)
        plate_src.store.keep_display_windows(
            [
                {"start": 0.0, "end": 59409.0, "min": 0.0, "max": 65535.0},
                {"start": 0.0, "end": 1000.0, "min": 0.0, "max": 65535.0},
            ]
        )
    finally:
        plate_src.close()

    from nd2wsi.plate import PlateStore

    old_manifest = PlateStore._read(plate_container)
    old_manifest["thumbs"].pop("channel_dtypes", None)
    old_manifest["created_by"] = {"nd2wsi_version": "2.6.1"}
    PlateStore._write_manifest(plate_container, old_manifest)

    reopened_plate = PlateSource(lif_path)
    try:
        assert reopened_plate.frame(0, 0, 0)[:, 0, 0].tolist() == [17, 1000]
        assert reopened_plate.reduced(0, 0, 0, 8)[:, 0, 0].tolist() == [17, 1000]
        assert reopened_plate.store.manifest["created_by"]["nd2wsi_version"] != "2.6.1"
        assert reopened_plate.store.manifest["thumbs"]["channel_dtypes"] == ["uint8", "uint16"]
        assert reopened_plate.attrs["omero"]["channels"][0]["window"]["end"] < 1000.0
        assert ann_canon.read_bytes() == ann_bytes
    finally:
        reopened_plate.close()

    # Build a managed pyramid cache and mutate it into the 2.6.1 state
    pyr_cache = ensure_cache(lif_path, tile=32)
    root0, attrs0 = open_store(pyr_cache)
    try:
        pyr_ann = annotations_sidecar(pyr_cache, attrs0)
        pyr_ann_bytes = b'{"items":[{"id":"roi-lif-pyr"}]}'
        pyr_ann.write_bytes(pyr_ann_bytes)
    finally:
        root0.close()
    zattrs = json.loads(read_entry(pyr_cache, "store.ome.zarr/.zattrs").decode("utf-8"))
    zattrs["nd2wsi"].pop("channel_dtypes", None)
    write_entry(pyr_cache, "store.ome.zarr/.zattrs", json.dumps(zattrs).encode("utf-8"))

    rebuilt_pyr = ensure_cache(lif_path, tile=32)
    root1, attrs1 = open_store(rebuilt_pyr)
    try:
        assert np.asarray(root1["0"])[:, 0, 0].tolist() == [17, 1000]
        assert attrs1["nd2wsi"]["channel_dtypes"] == ["uint8", "uint16"]
        assert pyr_ann.read_bytes() == pyr_ann_bytes
    finally:
        root1.close()

    # 2. Sparse CZI: simulate a 2.6.1 managed cache and legacy portable pyramid
    # that recorded "DAPI" instead of "EGFP" for SubBlock channel ID 1.
    czi_path = build_synthetic_czi(
        tmp_path / "sparse_upgrade.czi",
        {(0, 1, 0): np.full((32, 32), 500, np.uint16)},
        channels=[("DAPI", "#FF0000FF"), ("EGFP", "#FF00FF00")],
    )
    legacy_czi_store = default_store_path(czi_path)
    convert(czi_path, legacy_czi_store, tile=32, progress=False)
    leg_zattrs_path = legacy_czi_store / ".zattrs"
    leg_zattrs = json.loads(leg_zattrs_path.read_text(encoding="utf-8"))
    leg_zattrs["omero"]["channels"][0]["label"] = "DAPI"
    leg_zattrs["omero"]["channels"][0]["color"] = "0000FF"
    leg_zattrs_path.write_text(json.dumps(leg_zattrs), encoding="utf-8")

    czi_cache = ensure_cache(czi_path, tile=32)
    assert czi_cache != legacy_czi_store
    assert legacy_czi_store.exists()
    czi_zattrs = json.loads(read_entry(czi_cache, "store.ome.zarr/.zattrs").decode("utf-8"))
    czi_zattrs["omero"]["channels"][0]["label"] = "DAPI"
    czi_zattrs["omero"]["channels"][0]["color"] = "0000FF"
    write_entry(czi_cache, "store.ome.zarr/.zattrs", json.dumps(czi_zattrs).encode("utf-8"))

    czi_cache_rebuilt = ensure_cache(czi_path, tile=32)
    root_czi, attrs_czi = open_store(czi_cache_rebuilt)
    try:
        assert [c["label"] for c in attrs_czi["omero"]["channels"]] == ["EGFP"]
        assert [c["color"] for c in attrs_czi["omero"]["channels"]] == ["00FF00"]
    finally:
        root_czi.close()


def test_p2_czi_coherent_whole_list_channel_mapping_never_duplicates_identities():
    import xml.etree.ElementTree as ET

    from nd2wsi.czi import _extract_czi_metadata

    # Compact 2-channel metadata without numeric IDs and actual_c_values=[1, 2]
    xml_two = ET.fromstring(
        '<ImageDocument><Metadata><DisplaySetting><Channels>'
        '<Channel Name="EGFP" Color="#FF00FF00"/>'
        '<Channel Name="CY5" Color="#FFFF0000"/>'
        '</Channels></DisplaySetting></Metadata></ImageDocument>'
    )
    _, _, _, chs_two = _extract_czi_metadata(xml_two, 2, False, c_values=[1, 2])
    assert [c.name for c in chs_two] == ["EGFP", "CY5"]
    assert [c.color for c in chs_two] == [(0, 255, 0), (255, 0, 0)]

    # Full 3-channel table without numeric IDs and sparse actual_c_values=[0, 2]
    xml_three = ET.fromstring(
        '<ImageDocument><Metadata><DisplaySetting><Channels>'
        '<Channel Name="DAPI" Color="#FF0000FF"/>'
        '<Channel Name="EGFP" Color="#FF00FF00"/>'
        '<Channel Name="CY5" Color="#FFFF0000"/>'
        '</Channels></DisplaySetting></Metadata></ImageDocument>'
    )
    _, _, _, chs_sparse = _extract_czi_metadata(xml_three, 2, False, c_values=[0, 2])
    assert [c.name for c in chs_sparse] == ["DAPI", "CY5"]
    assert [c.color for c in chs_sparse] == [(0, 0, 255), (255, 0, 0)]

    # Ambiguous table (3 no-ID XML channels, actual_c_values=[1, 5] where 5 is out of bounds)
    _, _, _, chs_ambig = _extract_czi_metadata(xml_three, 2, False, c_values=[1, 5])
    assert [c.name for c in chs_ambig] == ["Channel 1", "Channel 5"]
    assert len({c.color for c in chs_ambig}) == 2


def test_pixels_1_1_lif_calibration_uses_n_minus_1_intervals(tmp_path):
    from contextlib import ExitStack

    from nd2wsi.lif import collect_lif_info, open_lif

    w, h, nz = 32, 32, 3
    lif_path = build_synthetic_lif(
        tmp_path / "cal.lif",
        [
            {
                "payload": np.zeros((nz, h, w), dtype=np.uint8).tobytes(),
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="1" Length="0.000031" Unit="m"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="32" Length="0.000031" Unit="m"/>',
                    f'<DimensionDescription DimID="3" NumberOfElements="{nz}" BytesInc="1024" Length="0.000008" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" Resolution="8" BytesInc="0"/>',
                ],
            }
        ],
    )
    with ExitStack() as stack:
        src = open_lif(stack, lif_path)
        assert np.allclose(src.pixel_size_um, (1.0, 1.0))

    info = collect_lif_info(lif_path)
    assert np.allclose(info["pixel_size_um"], [1.0, 1.0])

    plate = PlateSource(lif_path, store=False)
    try:
        assert np.allclose(plate.pixel_size_um, (1.0, 1.0))
        assert np.isclose(plate.z_step_um, 4.0)
    finally:
        plate.close()


def test_pixels_1_2_and_1_3_czi_bgr96float_bgra32_and_mixed_depth(tmp_path):
    from contextlib import ExitStack

    from nd2wsi.czi import collect_czi_info, open_czi
    from nd2wsi.render import composite

    # 1. PixelType 8 = Bgr96Float (float32 RGB -> stored as BGR -> decoded as RGB)
    rgb_f32 = np.zeros((32, 32, 3), dtype=np.float32)
    rgb_f32[:, :] = [0.75, 0.5, 0.25]  # R=0.75, G=0.5, B=0.25
    czi_f32 = build_synthetic_czi(
        tmp_path / "float_rgb.czi",
        {(0, 0, 0): rgb_f32},
        pixel_type=8,
    )
    with ExitStack() as stack:
        src_f32 = open_czi(stack, czi_f32)
        assert src_f32.rgb is True
        assert src_f32.dtype == np.dtype("float32")
        px_f32 = np.asarray(src_f32.data[:, 0, 0])
        assert np.allclose(px_f32, [0.75, 0.5, 0.25])

    info_f32 = collect_czi_info(czi_f32)
    assert info_f32["pixel_type"] == "Bgr96Float"
    assert info_f32["dtype"] == "float32"

    cache_f32 = ensure_cache(czi_f32, tile=32)
    root_f32, attrs_f32 = open_store(cache_f32)
    try:
        assert attrs_f32["nd2wsi"]["dtype"] == "float32"
        assert np.allclose(np.asarray(root_f32["0"])[:, 0, 0], [0.75, 0.5, 0.25])
        wins_f32 = [(c["window"]["start"], c["window"]["end"]) for c in attrs_f32["omero"]["channels"]]
        rgb_u8 = composite(np.asarray(root_f32["0"]), [0, 1, 2], wins_f32, [(255, 0, 0), (0, 255, 0), (0, 0, 255)], True)
        assert rgb_u8.dtype == np.uint8
        assert rgb_u8[0, 0].tolist() == [255, 127, 0]
    finally:
        root_f32.close()

    # 2. PixelType 9 = Bgra32 (uint8 RGBA -> stored as BGRA -> decoded as RGB)
    rgba_u8 = np.zeros((32, 32, 4), dtype=np.uint8)
    rgba_u8[:, :] = [30, 20, 10, 255]  # R=30, G=20, B=10, A=255
    czi_bgra = build_synthetic_czi(
        tmp_path / "bgra.czi",
        {(0, 0, 0): rgba_u8},
        pixel_type=9,
    )
    with ExitStack() as stack:
        src_bgra = open_czi(stack, czi_bgra)
        assert src_bgra.rgb is True
        assert src_bgra.dtype == np.dtype("uint8")
        assert np.asarray(src_bgra.data[:, 0, 0]).tolist() == [30, 20, 10]

    # 3. Mixed-depth CZI channels: Gray8 (17) + Gray16 (1000)
    czi_mixed = build_synthetic_czi(
        tmp_path / "mixed_depth.czi",
        {
            (0, 0, 0): np.full((32, 32), 17, dtype=np.uint8),
            (1, 0, 0): np.full((32, 32), 17, dtype=np.uint8),
            (0, 1, 0): np.full((32, 32), 1000, dtype=np.uint16),
            (1, 1, 0): np.full((32, 32), 1000, dtype=np.uint16),
        },
        pixel_type={0: 0, 1: 1},
    )
    with ExitStack() as stack:
        src_mixed = open_czi(stack, czi_mixed)
        assert src_mixed.dtype == np.dtype("uint16")
        assert src_mixed.channel_dtypes == ["uint8", "uint16"]
        assert np.asarray(src_mixed.data[:, 0, 0]).tolist() == [17, 1000]

    plate_mixed = PlateSource(czi_mixed, store=False)
    try:
        assert plate_mixed.dtype == np.dtype("uint16")
        assert plate_mixed.frame(0, 0, 0)[:, 0, 0].tolist() == [17, 1000]
        assert plate_mixed.frame(0, 0, 0)[:, 0, 1].tolist() == [17, 1000]
    finally:
        plate_mixed.close()

    cache_mixed = ensure_cache(czi_mixed, tile=32)
    root_m, attrs_m = open_store(cache_mixed)
    try:
        assert attrs_m["nd2wsi"]["channel_dtypes"] == ["uint8", "uint16"]
        assert np.asarray(root_m["0"])[:, 0, 0].tolist() == [17, 1000]
    finally:
        root_m.close()


def test_identity_1_1_1_2_and_1_3_direct_cache_open_time_channels_and_cli_source_identity(
    tmp_path, monkeypatch, capsys
):
    from nd2wsi import cli, server
    from nd2wsi.convert import convert, ensure_cache, open_store
    from nd2wsi.server import SlideRegistry
    from nd2wsi.storage.single_file import read_entry, write_entry

    # identity-1-1: Direct open of an outdated .nd2svs cache via SlideRegistry.open_path
    # rebuilds in place when the original source is available.
    czi_sparse = build_synthetic_czi(
        tmp_path / "direct_sparse.czi",
        {(0, 1, 0): np.full((32, 32), 7, np.uint16)},
        channels=[("DAPI", "#FF0000FF"), ("EGFP", "#FF00FF00")],
    )
    sparse_cache = ensure_cache(czi_sparse, tile=32)
    zattrs = json.loads(read_entry(sparse_cache, "store.ome.zarr/.zattrs").decode("utf-8"))
    zattrs["omero"]["channels"][0]["label"] = "DAPI"
    zattrs["omero"]["channels"][0]["color"] = "0000FF"
    write_entry(sparse_cache, "store.ome.zarr/.zattrs", json.dumps(zattrs).encode("utf-8"))

    reg = SlideRegistry()
    try:
        sid = reg.open_path(sparse_cache)
        st = reg.get(sid)
        assert [c["label"] for c in st.attrs["omero"]["channels"]] == ["EGFP"]
        assert np.asarray(st.root["0"])[:, 0, 0].tolist() == [7]
    finally:
        reg.close_all(immediate=True)

    # identity-1-2: CZI time-dependent channels (t=0 has C=0, t=1 has C=0,1)
    variable_czi = build_synthetic_czi(
        tmp_path / "variable_channels.czi",
        {
            (0, 0, 0, 0): np.full((32, 32), 11, np.uint16),
            (1, 0, 0, 0): np.full((32, 32), 22, np.uint16),
            (1, 0, 1, 0): np.full((32, 32), 33, np.uint16),
        },
        channels=[("DAPI", "#FF0000FF"), ("EGFP", "#FF00FF00")],
    )
    cache_t0 = ensure_cache(variable_czi, selection=PlaneSelection(t=0), tile=32)
    root_t0, attrs_t0 = open_store(cache_t0)
    try:
        assert [c["label"] for c in attrs_t0["omero"]["channels"]] == ["DAPI"]
        assert np.asarray(root_t0["0"])[:, 0, 0].tolist() == [11]
    finally:
        root_t0.close()

    cache_t1 = ensure_cache(variable_czi, selection=PlaneSelection(t=1), tile=32)
    root_t1, attrs_t1 = open_store(cache_t1)
    try:
        assert [c["label"] for c in attrs_t1["omero"]["channels"]] == ["DAPI", "EGFP"]
        assert np.asarray(root_t1["0"])[:, 0, 0].tolist() == [22, 33]
    finally:
        root_t1.close()

    # identity-1-3: Explicit CLI store reuse must reject a store built from a different source
    slide_a = build_synthetic_czi(
        tmp_path / "A.czi",
        {(0, 0, 0): np.full((32, 32), 111, np.uint16)},
    )
    slide_b = build_synthetic_czi(
        tmp_path / "B.czi",
        {(0, 0, 0): np.full((32, 32), 222, np.uint16)},
    )
    explicit_store = tmp_path / "portable.ome.zarr"
    convert(slide_a, explicit_store, tile=32, progress=False)

    served: list[Path] = []
    monkeypatch.setattr(server, "serve", lambda stores, **kw: served.extend(stores))
    rc = cli.main(
        ["view", str(slide_b), "--store", str(explicit_store), "--tile", "32", "--yes"]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert served == []
    assert "different source" in err

    # Same-filename older explicit store lacking source_fingerprint must also be refused
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()
    scan_a = build_synthetic_czi(dir_a / "scan.czi", {(0, 0, 0): np.full((32, 32), 11, np.uint16)})
    scan_b = build_synthetic_czi(dir_b / "scan.czi", {(0, 0, 0): np.full((32, 32), 99, np.uint16)})
    old_portable = tmp_path / "old_portable.ome.zarr"
    convert(scan_a, old_portable, tile=32, progress=False)
    old_zattrs_path = old_portable / ".zattrs"
    old_zattrs = json.loads(old_zattrs_path.read_text(encoding="utf-8"))
    old_zattrs["nd2wsi"].pop("source_fingerprint", None)
    old_zattrs_path.write_text(json.dumps(old_zattrs), encoding="utf-8")

    rc_same_name = cli.main(
        ["view", str(scan_b), "--store", str(old_portable), "--tile", "32", "--yes"]
    )
    err_same_name = capsys.readouterr().err
    assert rc_same_name == 1
    assert served == []
    assert "different source" in err_same_name

    # Mixed SubBlock types within a single CZI channel across Z (Z=0 Gray16, Z=1 Gray8)
    import struct

    from nd2wsi.plate import PlateStore

    mixed_within = build_synthetic_czi(
        tmp_path / "mixed_within_channel.czi",
        {
            (0, 0, 0): np.full((32, 32), 1000, np.uint16),
            (1, 0, 0): np.full((32, 32), 17, np.uint8),
        },
        pixel_type=1,
    )
    buf = bytearray(mixed_within.read_bytes())
    directory = struct.unpack_from("<Q", buf, 84)[0]
    pos = directory + 160
    for ptype in (1, 0):
        fpos = struct.unpack_from("<q", buf, pos + 6)[0]
        ndims = struct.unpack_from("<i", buf, pos + 28)[0]
        struct.pack_into("<i", buf, pos + 2, ptype)
        struct.pack_into("<i", buf, fpos + 32 + 16 + 2, ptype)
        pos += 32 + ndims * 20
    mixed_within.write_bytes(buf)

    plate_mw = PlateSource(mixed_within)
    try:
        plate_mw_container = plate_mw.store.container
        plate_mw.store.put(0, 0, 1, np.full((1, 4, 4), 4369, dtype=np.uint16))
    finally:
        plate_mw.close()
    mw_manifest = PlateStore._read(plate_mw_container)
    mw_manifest["thumbs"].pop("channel_dtypes", None)
    PlateStore._write_manifest(plate_mw_container, mw_manifest)

    reopened_mw = PlateSource(mixed_within)
    try:
        assert int(reopened_mw.frame(0, 0, 1)[0, 0, 0]) == 17
        assert int(reopened_mw.reduced(0, 0, 1, 8)[0, 0, 0]) == 17
        assert reopened_mw._channel_dtypes == ["uint16+uint8"]
    finally:
        reopened_mw.close()

    from nd2wsi.cache import read_manifest

    mw_cache = ensure_cache(mixed_within, selection=PlaneSelection(z=1), tile=32)
    mw_zattrs = json.loads(read_entry(mw_cache, "store.ome.zarr/.zattrs").decode("utf-8"))
    mw_zattrs["nd2wsi"].pop("channel_dtypes", None)
    write_entry(mw_cache, "store.ome.zarr/.zattrs", json.dumps(mw_zattrs).encode("utf-8"))
    old_gen = read_manifest(mw_cache)["generation"]

    mw_rebuilt = ensure_cache(mixed_within, selection=PlaneSelection(z=1), tile=32)
    new_gen = read_manifest(mw_rebuilt)["generation"]
    assert new_gen != old_gen
    root_mw, attrs_mw = open_store(mw_rebuilt)
    try:
        assert int(np.asarray(root_mw["0"])[0, 0, 0]) == 17
        assert attrs_mw["nd2wsi"]["channel_dtypes"] == ["uint16+uint8"]
    finally:
        root_mw.close()



