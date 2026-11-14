"""Regression tests for 2.6.3 cache, pyramid, plate, CLI, and export fixes (CPE-1..CPE-8)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import struct
import urllib.error
import urllib.request
from pathlib import Path

import imagecodecs
import numpy as np
import pytest
import tifffile
import zarr
from http_fixture import running_server, serving
from nd2_fixture import write_synthetic_nd2

import nd2wsi.convert as convert_mod
from nd2wsi.app import open_or_convert
from nd2wsi.cache import (
    CORRECTED_SINGLE_FILE_FORMAT,
    MANIFEST_NAME,
    SINGLE_FILE_FORMAT,
    cache_container,
    cache_matches,
    quick_fingerprint,
    read_manifest,
)
from nd2wsi.cache_removal import _validate_file_cache_for_trash
from nd2wsi.cli import main as cli_main
from nd2wsi.convert import (
    _legacy_store,
    _store_matches_source_semantics,
    convert,
    ensure_cache,
    existing_cache_store,
    open_store,
)
from nd2wsi.h_correction import _basic_reason
from nd2wsi.h_persistence import PreparedReplacement
from nd2wsi.migration import cache_entries, pack_directory
from nd2wsi.plate import THUMB_K, PlateSource, PlateStore, plate_container
from nd2wsi.reader import PlaneSelection
from nd2wsi.server import SlideRegistry, create_server, server_url
from nd2wsi.storage.single_file import is_single_file, read_entry, write_entry
from nd2wsi.storage.zarr_v2 import ZarrV2ChunkArray, ZarrV2Storage


def _store_attrs(container: Path) -> dict:
    """Open a Zarr store, read attrs, and close any SQLite handles immediately."""
    root, attrs = open_store(container)
    close = getattr(root, "close", None)
    if close is not None:
        close()
    return attrs


@pytest.fixture(autouse=True)
def _disable_plate_bg_builder(monkeypatch):
    monkeypatch.setattr(PlateStore, "start", lambda self: None)


def _write_lif_raw(path: Path, inner_xml: str, mem_blocks: list[tuple[str, bytes]]) -> Path:
    xml_str = (
        '<?xml version="1.0" encoding="utf-16"?>'
        f'<LMSDataContainerHeader Version="2">{inner_xml}</LMSDataContainerHeader>'
    )
    xml_utf16 = xml_str.encode("utf-16-le")
    buf = bytearray()
    buf.extend(struct.pack("<iIBI", 0x70, 5 + len(xml_utf16), 0x2A, len(xml_utf16) // 2))
    buf.extend(xml_utf16)
    for mem_id, payload in mem_blocks:
        id_utf16 = mem_id.encode("utf-16-le")
        chunk_len = 1 + 8 + 1 + 4 + len(id_utf16)
        buf.extend(
            struct.pack("<iIBQBI", 0x70, chunk_len, 0x2A, len(payload), 0x2A, len(id_utf16) // 2)
        )
        buf.extend(id_utf16)
        buf.extend(payload)
    path.write_bytes(bytes(buf))
    return path


def _build_czi_custom(
    path: Path,
    subblocks: list[dict],
    *,
    pixel_size_um: tuple[float, float] | None = (0.25, 0.25),
    magnification: float = 20.0,
    channels: list[tuple[str, str]] | None = None,
) -> Path:
    """Build a synthetic CZI with explicit per-SubBlock (s, t, z, c, m, x, y, pixel_type, compression)."""
    dir_records: list[tuple] = []
    buf = bytearray(512)

    for sb in subblocks:
        arr: np.ndarray = sb["data"]
        s_val = int(sb.get("s", 0))
        t_val = int(sb.get("t", 0))
        z_val = int(sb.get("z", 0))
        c_val = int(sb.get("c", 0))
        m_val = int(sb.get("m", 0))
        xs = int(sb.get("x", 0))
        ys = int(sb.get("y", 0))
        ptype = int(sb.get("pixel_type", 1))
        comp = int(sb.get("compression", 0))

        if arr.ndim == 2:
            h, w = arr.shape
            raw_arr = arr
        else:
            h, w, s_ch = arr.shape
            raw_arr = arr[:, :, [2, 1, 0, 3]] if s_ch == 4 else arr[:, :, ::-1]
        raw_bytes = np.ascontiguousarray(raw_arr).tobytes()
        if comp == 0:
            payload = raw_bytes
        elif comp == 6:
            u8 = np.frombuffer(raw_bytes, dtype=np.uint8)
            lo = u8[0::2]
            hi = u8[1::2]
            packed = np.concatenate([lo, hi]).tobytes()
            payload = bytes([3, 1, 1]) + imagecodecs.zstd_encode(packed)
        else:
            raise ValueError(f"unsupported synthetic compression {comp}")

        fpos = len(buf)
        dims = [
            (b"X\x00\x00\x00", xs, w),
            (b"Y\x00\x00\x00", ys, h),
            (b"C\x00\x00\x00", c_val, 1),
            (b"Z\x00\x00\x00", z_val, 1),
            (b"T\x00\x00\x00", t_val, 1),
            (b"S\x00\x00\x00", s_val, 1),
            (b"M\x00\x00\x00", m_val, 1),
        ]
        ndims = len(dims)
        hdr_len = max(256, 16 + 32 + 20 * ndims)
        used_size = hdr_len + len(payload)
        alloc_size = (used_size + 31) // 32 * 32

        seg_hdr = struct.pack("<16sQQ", b"ZISRAWSUBBLOCK\x00\x00", alloc_size, used_size)
        sb_prefix = bytearray(hdr_len)
        struct.pack_into("<IIQ", sb_prefix, 0, 0, 0, len(payload))
        struct.pack_into(
            "<2siqiiBB4si",
            sb_prefix,
            16,
            b"DV",
            ptype,
            fpos,
            0,
            comp,
            0,
            0,
            b"\x00" * 4,
            ndims,
        )
        for idx, (dname, st_i, sz_i) in enumerate(dims):
            struct.pack_into(
                "<4siifi", sb_prefix, 48 + idx * 20, dname, st_i, sz_i, float(st_i), sz_i
            )
        buf.extend(seg_hdr)
        buf.extend(sb_prefix)
        buf.extend(payload)
        if alloc_size > used_size:
            buf.extend(b"\x00" * (alloc_size - used_size))
        dir_records.append((fpos, ptype, comp, dims))

    dir_pos = len(buf)
    dir_payload = bytearray(128)
    struct.pack_into("<i", dir_payload, 0, len(dir_records))
    for fpos, ptype, comp, dims in dir_records:
        dir_payload.extend(
            struct.pack(
                "<2siqiiBB4si",
                b"DV",
                ptype,
                fpos,
                0,
                comp,
                0,
                0,
                b"\x00" * 4,
                len(dims),
            )
        )
        for dname, st_i, sz_i in dims:
            dir_payload.extend(struct.pack("<4siifi", dname, st_i, sz_i, float(st_i), sz_i))
    dir_used = len(dir_payload)
    dir_alloc = (dir_used + 31) // 32 * 32
    buf.extend(struct.pack("<16sQQ", b"ZISRAWDIRECTORY\x00", dir_alloc, dir_used))
    buf.extend(dir_payload)
    if dir_alloc > dir_used:
        buf.extend(b"\x00" * (dir_alloc - dir_used))

    meta_pos = len(buf)
    ch_nodes = ""
    for idx, (cname, ccolor) in enumerate(channels or [("DAPI", "#FF00A0FF")]):
        ch_nodes += (
            f'<Channel Id="Channel:{idx}" Name="{cname}">'
            f"<ShortName>{cname}</ShortName><Color>{ccolor}</Color></Channel>"
        )
    scaling_xml = ""
    if pixel_size_um is not None:
        py_m = pixel_size_um[0] * 1e-6
        px_m = pixel_size_um[1] * 1e-6
        scaling_xml = (
            "<Scaling><Items>"
            f'<Distance Id="X"><Value>{px_m}</Value></Distance>'
            f'<Distance Id="Y"><Value>{py_m}</Value></Distance>'
            "</Items></Scaling>"
        )
    xml_bytes = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<ImageDocument><Metadata>"
        "<Information><Instrument><Objectives>"
        f'<Objective Id="Obj:1"><NominalMagnification>{magnification}</NominalMagnification></Objective>'
        "</Objectives></Instrument></Information>"
        f"{scaling_xml}"
        f"<DisplaySetting><Channels>{ch_nodes}</Channels></DisplaySetting>"
        "</Metadata></ImageDocument>"
    ).encode()
    meta_hdr = bytearray(256)
    struct.pack_into("<II", meta_hdr, 0, len(xml_bytes), 0)
    meta_used = 256 + len(xml_bytes)
    meta_alloc = (meta_used + 31) // 32 * 32
    buf.extend(struct.pack("<16sQQ", b"ZISRAWMETADATA\x00\x00", meta_alloc, meta_used))
    buf.extend(meta_hdr)
    buf.extend(xml_bytes)
    if meta_alloc > meta_used:
        buf.extend(b"\x00" * (meta_alloc - meta_used))

    struct.pack_into("<16sQQ", buf, 0, b"ZISRAWFILE\x00\x00\x00\x00\x00\x00", 480, 80)
    struct.pack_into(
        "<iiii16s16siQQiQ",
        buf,
        32,
        1,
        0,
        0,
        0,
        b"\x01" * 16,
        b"\x02" * 16,
        0,
        dir_pos,
        meta_pos,
        0,
        0,
    )
    path.write_bytes(bytes(buf))
    return path


def _mutate_cached_zattrs(container: Path, mutator) -> None:
    """Mutate store.ome.zarr/.zattrs inside a .nd2svs container while keeping manifest fingerprint."""
    raw = json.loads(
        zarr.open_group  # dummy ref for clarity
        and __import__("nd2wsi.storage.single_file", fromlist=["read_entry"])
        .read_entry(container, "store.ome.zarr/.zattrs")
        .decode("utf-8")
    )
    mutator(raw)
    write_entry(container, "store.ome.zarr/.zattrs", json.dumps(raw).encode("utf-8"))


def _sync_manifest_fingerprint(container: Path, slide: Path) -> None:
    manifest = read_manifest(container)
    assert manifest is not None
    fp = quick_fingerprint(slide)
    manifest["source"].update(fp)
    write_entry(container, MANIFEST_NAME, json.dumps(manifest).encode("utf-8"))


# ---------------------------------------------------------------------------
# CPE-1 + PF-3b + PF-1 cache tag
# ---------------------------------------------------------------------------


def test_cpe1_corrupt_261_cache_whose_source_now_raises_is_quarantined(tmp_path: Path):
    w, h = 64, 64
    valid_lif = tmp_path / "int32_bug.lif"
    good_xml = (
        '<Element Name="S0"><Data><Image><ImageDescription>'
        '<Channels><ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Green" BytesInc="0"/></Channels>'
        f'<Dimensions><DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="2" Length="6.3e-5" Unit="m"/>'
        f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w * 2}" Length="6.3e-5" Unit="m"/></Dimensions>'
        '</ImageDescription></Image></Data>'
        f'<Memory Size="{w * h * 2}" MemoryBlockID="Mem_1"/></Element>'
    )
    _write_lif_raw(valid_lif, good_xml, [("Mem_1", np.ones((h, w), dtype=np.uint16).tobytes())])
    container = ensure_cache(valid_lif)
    attrs = _store_attrs(container)
    assert _store_matches_source_semantics(valid_lif, PlaneSelection(), attrs)

    # Now rewrite the LIF as an unsupported 32-bit integer (DataType="0" Resolution="32")
    # and forge a 2.6.1 cache manifest matching the rewritten file's fingerprint.
    bad_xml = (
        '<Element Name="S0"><Data><Image><ImageDescription>'
        '<Channels><ChannelDescription DataType="0" ChannelTag="0" Resolution="32" LUTName="Green" BytesInc="0"/></Channels>'
        f'<Dimensions><DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="4" Length="6.3e-5" Unit="m"/>'
        f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w * 4}" Length="6.3e-5" Unit="m"/></Dimensions>'
        '</ImageDescription></Image></Data>'
        f'<Memory Size="{w * h * 4}" MemoryBlockID="Mem_1"/></Element>'
    )
    _write_lif_raw(valid_lif, bad_xml, [("Mem_1", np.ones((h, w), dtype=np.uint32).tobytes())])
    _sync_manifest_fingerprint(container, valid_lif)

    # Semantics check must return False rather than swallowing NotImplementedError/ValueError!
    assert _store_matches_source_semantics(valid_lif, PlaneSelection(), attrs) is False

    # B1 Step 3: direct ensure_cache keeps the old container in place on rebuild failure.
    with pytest.raises((ValueError, NotImplementedError)):
        ensure_cache(valid_lif)
    assert container.exists()
    assert not list(container.parent.glob(f"{container.name}.corrupt-*"))

    # B1: SlideRegistry.open_path on a matching-fingerprint unreadable source still fails closed,
    # quarantines the corrupt cache, and names the set-aside file in the error.
    reg = SlideRegistry()
    try:
        with pytest.raises(ValueError, match=r"\.corrupt-"):
            reg.open_path(container)
    finally:
        reg.close_all(immediate=True)
    assert not container.exists()
    assert len(list(container.parent.glob(f"{container.name}.corrupt-*"))) == 1


def _zstd1_slide_with_262_cache(tmp_path: Path) -> tuple[Path, Path, np.ndarray]:
    rng = np.random.default_rng(7)
    ch0 = rng.integers(100, 4000, (128, 128), dtype=np.uint16)
    ch1 = rng.integers(100, 4000, (128, 128), dtype=np.uint16)
    slide = _build_czi_custom(
        tmp_path / "zstd1.czi",
        [
            {"data": ch0, "c": 0, "pixel_type": 1, "compression": 6},
            {"data": ch1, "c": 1, "pixel_type": 1, "compression": 6},
        ],
        channels=[("DAPI", "#FF00A0FF"), ("GFP", "#FF00FF00")],
    )
    store = ensure_cache(slide)
    key = "store.ome.zarr/.zattrs"
    attrs = json.loads(read_entry(store, key))
    # Simulate a 2.6.2 cache lacking ":zstd1" and "czi_scene_t"
    attrs["nd2wsi"]["channel_dtypes"] = ["uint16", "uint16"]
    attrs["nd2wsi"].pop("czi_scene_t", None)
    write_entry(store, key, json.dumps(attrs).encode("utf-8"))
    truth = np.stack([ch0, ch1])
    return slide, store, truth


def test_a2_valid_262_zstd1_cache_opens_from_read_only_folder(tmp_path: Path):
    slide, store, truth = _zstd1_slide_with_262_cache(tmp_path)
    (tmp_path / "nd2wsi" / "annotations").mkdir(parents=True, exist_ok=True)
    before = hashlib.sha256(store.read_bytes()).hexdigest()
    paths = [tmp_path, *tmp_path.rglob("*")]
    for p in paths:
        os.chmod(p, 0o555 if p.is_dir() else 0o444)
    try:
        if os.access(tmp_path, os.W_OK):
            pytest.skip("process bypasses POSIX permissions")
        reg = SlideRegistry()
        try:
            sid = reg.open_path(slide)
            root = reg.slides[sid].root
            assert np.array_equal(np.asarray(root["0"][:]), truth)
        finally:
            reg.close_all(immediate=True)
        assert hashlib.sha256(store.read_bytes()).hexdigest() == before
        assert not list(tmp_path.glob("*.corrupt-*"))
    finally:
        for p in paths:
            os.chmod(p, 0o755 if p.is_dir() else 0o644)


def test_b1_failed_rebuild_never_moves_readable_cache_aside(tmp_path: Path, monkeypatch):
    # Case 1: Zstd1 2.6.2 cache (reused without rebuild, or even if rebuild fails)
    (tmp_path / "z1").mkdir()
    slide_z, store_z, _ = _zstd1_slide_with_262_cache(tmp_path / "z1")
    before_z = hashlib.sha256(store_z.read_bytes()).hexdigest()

    # Case 2: PF-5 label mismatch forces ensure_cache to attempt a real rebuild
    (tmp_path / "pf5").mkdir()
    czi_pf5 = _build_czi_custom(
        tmp_path / "pf5" / "pf5_label.czi",
        [{"data": np.full((64, 64), 321, dtype=np.uint16), "c": 0, "pixel_type": 1}],
        channels=[("DAPI", "#FF00A0FF")],
    )
    store_pf5 = ensure_cache(czi_pf5)
    _mutate_cached_zattrs(
        store_pf5, lambda d: d["omero"]["channels"][0].update({"label": "Channel:0"})
    )
    before_pf5 = hashlib.sha256(store_pf5.read_bytes()).hexdigest()

    def no_space(*_a, **_k):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(convert_mod, "convert", no_space)
    for slide, container, before in (
        (slide_z, store_z, before_z),
        (czi_pf5, store_pf5, before_pf5),
    ):
        try:
            ensure_cache(slide)
        except OSError:
            pass
        assert container.exists()
        assert hashlib.sha256(container.read_bytes()).hexdigest() == before
        assert not list(container.parent.glob("*.corrupt-*"))
        assert _store_attrs(container)["nd2wsi"]["levels"]


def test_b1_unreadable_modified_source_serves_existing_nd2_and_czi_cache(tmp_path: Path):
    pytest.importorskip("limnd2")
    pytest.importorskip("nd2")

    rng = np.random.default_rng(101)
    nd2_arr = rng.integers(100, 4000, size=(64, 64, 1), dtype=np.uint16)
    nd2_src = write_synthetic_nd2(
        tmp_path / "scan.nd2", nd2_arr, [("DAPI", "blue")], pixel_um=0.5
    )
    nd2_cache = ensure_cache(nd2_src)
    nd2_cache_sha = hashlib.sha256(nd2_cache.read_bytes()).hexdigest()

    czi_arr = rng.integers(100, 4000, size=(64, 64), dtype=np.uint16)
    czi_src = _build_czi_custom(
        tmp_path / "scan.czi",
        [{"data": czi_arr, "pixel_type": 1, "compression": 6}],
        channels=[("DAPI", "#FF00A0FF")],
    )
    czi_cache = ensure_cache(czi_src)
    czi_cache_sha = hashlib.sha256(czi_cache.read_bytes()).hexdigest()

    def _assert_serves(container: Path, expected_sha: str, expected_plane: np.ndarray):
        reg = SlideRegistry()
        try:
            sid = reg.open_path(container)
            st = reg.get(sid)
            assert st is not None
            assert np.array_equal(np.asarray(st.root["0"][0]), expected_plane)
            assert any(
                "showing the cache as it was built" in n
                for n in (st.attrs["nd2wsi"].get("notes") or [])
            )
        finally:
            reg.close_all(immediate=True)

        httpd = create_server([], port=0)
        with serving(httpd):
            req = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}/api/open",
                data=json.dumps({"path": str(container)}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                assert resp.status == 200
                body = json.load(resp)
                assert body["sid"]
        assert hashlib.sha256(container.read_bytes()).hexdigest() == expected_sha
        assert not list(container.parent.glob(f"{container.name}.corrupt-*"))

    # Case A: damage bytes past the header while preserving file size and mtime
    for src, container, expected_sha, plane in (
        (nd2_src, nd2_cache, nd2_cache_sha, nd2_arr[:, :, 0]),
        (czi_src, czi_cache, czi_cache_sha, czi_arr),
    ):
        raw = bytearray(src.read_bytes())
        orig_stat = src.stat()
        mid = min(max(64, len(raw) // 2), len(raw))
        raw[mid:] = b"\xff" * (len(raw) - mid)
        src.write_bytes(bytes(raw))
        os.utime(src, ns=(orig_stat.st_atime_ns, orig_stat.st_mtime_ns))
        _assert_serves(container, expected_sha, plane)

    # Case B: truncate source files to 60%
    for src, container, expected_sha, plane in (
        (nd2_src, nd2_cache, nd2_cache_sha, nd2_arr[:, :, 0]),
        (czi_src, czi_cache, czi_cache_sha, czi_arr),
    ):
        raw = src.read_bytes()
        src.write_bytes(raw[: (len(raw) * 60) // 100])
        _assert_serves(container, expected_sha, plane)


def test_pf3b_scene_local_czi_timepoints_and_c10_legacy_t_marker(tmp_path: Path):
    rng = np.random.default_rng(21)
    p_s0 = rng.integers(100, 2000, size=(64, 64), dtype=np.uint16)
    p_s1 = rng.integers(300, 4000, size=(64, 64), dtype=np.uint16)

    # Scene 0 has T=0; Scene 1 starts at T=5 (no T=0 in Scene 1!), compressed with zstd1 (comp=6).
    czi_path = _build_czi_custom(
        tmp_path / "multi_scene_zstd1.czi",
        [
            {"data": p_s0, "s": 0, "t": 0, "z": 0, "c": 0, "m": 0, "pixel_type": 1, "compression": 6},
            {"data": p_s1, "s": 1, "t": 5, "z": 0, "c": 0, "m": 0, "pixel_type": 1, "compression": 6},
        ],
        channels=[("AF488", "#FF00FF00")],
    )
    sel_s1 = PlaneSelection(p=1, t=0)
    store_s1 = ensure_cache(czi_path, selection=sel_s1)
    root, attrs = open_store(store_s1)
    try:
        assert attrs["nd2wsi"]["channel_dtypes"] == ["uint16"]
        assert attrs["nd2wsi"]["czi_scene_t"] == [1, 5]
        assert np.array_equal(np.asarray(root["0"][0]), p_s1)
    finally:
        root.close()

    # Subsequent ensure_cache reuses the valid store (s_pick resolved before t_vals).
    assert ensure_cache(czi_path, selection=sel_s1) == store_s1

    # C10: 2.6.2 cache lacking "czi_scene_t" on Scene 1 (legacy global t_vals[0]=0 != 5) must be rejected and rebuilt!
    _mutate_cached_zattrs(store_s1, lambda d: d["nd2wsi"].pop("czi_scene_t", None))
    stale_attrs = _store_attrs(store_s1)
    assert _store_matches_source_semantics(czi_path, sel_s1, stale_attrs) is False

    rebuilt = ensure_cache(czi_path, selection=sel_s1)
    fresh_attrs = _store_attrs(rebuilt)
    assert fresh_attrs["nd2wsi"]["channel_dtypes"] == ["uint16"]
    assert fresh_attrs["nd2wsi"]["czi_scene_t"] == [1, 5]
    assert list(store_s1.parent.glob(f"{store_s1.name}.corrupt-*"))

    # C10: Conversely, Scene 0 (legacy global t_vals[0]=0 == scene T=0) without czi_scene_t is reused!
    sel_s0 = PlaneSelection(p=0, t=0)
    store_s0 = ensure_cache(czi_path, selection=sel_s0)
    _mutate_cached_zattrs(store_s0, lambda d: d["nd2wsi"].pop("czi_scene_t", None))
    assert _store_matches_source_semantics(czi_path, sel_s0, _store_attrs(store_s0)) is True
    assert ensure_cache(czi_path, selection=sel_s0) == store_s0


# ---------------------------------------------------------------------------
# CPE-2: dimension / pixel_size / magnification / legacy mtime checks
# ---------------------------------------------------------------------------


def test_cpe2_lif_hierarchical_child_bleed_dimensions_and_magnification_rejected(tmp_path: Path):
    w, h = 64, 64
    parent_arr = np.full((h, w), 1200, dtype=np.uint16)
    child_arr = np.full((4, h, w), 500, dtype=np.uint16)
    inner_xml = (
        '<Element Name="ParentOverview">'
        "<Data><Image>"
        '<Attachment Name="HardwareSetting">'
        '<ATLConfocalSettingDefinition Magnification="10"/>'
        "</Attachment>"
        "<ImageDescription>"
        '<Channels><ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Green" BytesInc="0"/></Channels>'
        f'<Dimensions><DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="2" Length="6.3e-5" Unit="m"/>'
        f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w * 2}" Length="6.3e-5" Unit="m"/></Dimensions>'
        "</ImageDescription></Image></Data>"
        f'<Memory Size="{parent_arr.nbytes}" MemoryBlockID="Mem_Parent"/>'
        '<Children><Element Name="ChildTileScan"><Data><Image>'
        '<Attachment Name="HardwareSetting"><ATLConfocalSettingDefinition Magnification="63"/></Attachment>'
        '<Attachment Name="TileScanInfo">'
        '<Tile FieldX="0" FieldY="0"/><Tile FieldX="1" FieldY="0"/>'
        '<Tile FieldX="0" FieldY="1"/><Tile FieldX="1" FieldY="1"/>'
        "</Attachment>"
        "<ImageDescription>"
        '<Channels><ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Green" BytesInc="0"/></Channels>'
        f'<Dimensions><DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="2" Length="6.3e-5" Unit="m"/>'
        f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w * 2}" Length="6.3e-5" Unit="m"/>'
        f'<DimensionDescription DimID="10" NumberOfElements="4" BytesInc="{w * h * 2}" Length="4" Unit=""/></Dimensions>'
        "</ImageDescription></Image></Data>"
        f'<Memory Size="{child_arr.nbytes}" MemoryBlockID="Mem_Child"/></Element></Children>'
        "</Element>"
    )
    lif_path = _write_lif_raw(
        tmp_path / "hier.lif",
        inner_xml,
        [("Mem_Parent", parent_arr.tobytes()), ("Mem_Child", child_arr.tobytes())],
    )
    container = ensure_cache(lif_path, selection=PlaneSelection(p=0))
    good_attrs = _store_attrs(container)
    assert good_attrs["nd2wsi"]["levels"][0]["width"] == w
    assert good_attrs["nd2wsi"]["objective_magnification"] == pytest.approx(10.0)

    # Simulate 2.6.2 child-bleed magnification (63x instead of 10x):
    _mutate_cached_zattrs(
        container, lambda d: d["nd2wsi"].update({"objective_magnification": 63.0})
    )
    bled_mag_attrs = _store_attrs(container)
    assert _store_matches_source_semantics(lif_path, PlaneSelection(p=0), bled_mag_attrs) is False

    # Simulate 2.6.2 child-bleed TileScan dimensions (128x128 instead of 64x64):
    _mutate_cached_zattrs(
        container,
        lambda d: (
            d["nd2wsi"].update({"objective_magnification": 10.0}),
            d["nd2wsi"]["levels"][0].update({"width": 128, "height": 128}),
        ),
    )
    bled_dim_attrs = _store_attrs(container)
    assert _store_matches_source_semantics(lif_path, PlaneSelection(p=0), bled_dim_attrs) is False

    rebuilt = ensure_cache(lif_path, selection=PlaneSelection(p=0))
    fixed_attrs = _store_attrs(rebuilt)
    assert fixed_attrs["nd2wsi"]["levels"][0]["width"] == 64
    assert fixed_attrs["nd2wsi"]["objective_magnification"] == pytest.approx(10.0)


def test_cpe2_czi_nd2_and_svs_metadata_validation(tmp_path: Path):
    pytest.importorskip("limnd2")
    pytest.importorskip("nd2")

    # 1. CZI pixel_size_um & width/height checks
    czi_path = _build_czi_custom(
        tmp_path / "cal.czi",
        [{"data": np.ones((64, 80), dtype=np.uint16) * 500, "pixel_type": 1}],
        pixel_size_um=(0.5, 0.5),
    )
    czi_cache = ensure_cache(czi_path)
    _mutate_cached_zattrs(czi_cache, lambda d: d["nd2wsi"].update({"pixel_size_um": [1.0, 1.0]}))
    bad_px_attrs = _store_attrs(czi_cache)
    assert _store_matches_source_semantics(czi_path, PlaneSelection(), bad_px_attrs) is False

    _mutate_cached_zattrs(
        czi_cache,
        lambda d: (
            d["nd2wsi"].update({"pixel_size_um": [0.5, 0.5]}),
            d["nd2wsi"]["levels"][0].update({"width": 999}),
        ),
    )
    bad_wh_attrs = _store_attrs(czi_cache)
    assert _store_matches_source_semantics(czi_path, PlaneSelection(), bad_wh_attrs) is False

    # 2. ND2 metadata checks
    nd2_path = write_synthetic_nd2(
        tmp_path / "scan.nd2",
        np.ones((64, 96, 2), dtype=np.uint16) * 300,
        [("DAPI", "blue"), ("GFP", "green")],
        pixel_um=0.4,
    )
    nd2_cache = ensure_cache(nd2_path)
    _mutate_cached_zattrs(nd2_cache, lambda d: d["nd2wsi"].update({"pixel_size_um": [0.8, 0.8]}))
    bad_nd2_attrs = _store_attrs(nd2_cache)
    assert _store_matches_source_semantics(nd2_path, PlaneSelection(), bad_nd2_attrs) is False

    # 3. SVS metadata checks
    svs_path = tmp_path / "scan.svs"
    rng = np.random.default_rng(7)
    svs_img = rng.integers(0, 255, (256, 320, 3), dtype=np.uint8)
    tifffile.imwrite(
        svs_path,
        svs_img,
        tile=(128, 128),
        photometric="rgb",
        compression="jpeg2000",
        compressionargs={"reversible": True},
        description="Aperio Image|MPP = 0.25|AppMag = 40",
    )
    svs_cache = ensure_cache(svs_path)
    _mutate_cached_zattrs(svs_cache, lambda d: d["nd2wsi"].update({"pixel_size_um": [0.5, 0.5]}))
    bad_svs_attrs = _store_attrs(svs_cache)
    assert _store_matches_source_semantics(svs_path, PlaneSelection(), bad_svs_attrs) is False


def test_cpe2_legacy_unfingerprinted_store_refused_without_deletion_when_slide_overwritten(
    tmp_path: Path,
):
    svs_path = tmp_path / "legacy_scan.svs"
    img1 = np.full((256, 256, 3), 40, dtype=np.uint8)
    tifffile.imwrite(
        svs_path,
        img1,
        tile=(128, 128),
        photometric="rgb",
        compression="jpeg2000",
        compressionargs={"reversible": True},
        description="Aperio Image|MPP = 0.5|AppMag = 20",
    )
    legacy = tmp_path / "pyramids" / "legacy_scan.ome.zarr"
    convert(svs_path, legacy, progress=False)

    # Strip source_fingerprint to simulate a pre-2.6.2 portable store built after svs_path.
    g = zarr.open_group(str(legacy), mode="r+")
    meta = dict(g.attrs["nd2wsi"])
    meta.pop("source_fingerprint", None)
    g.attrs["nd2wsi"] = meta

    zattrs_ns = (legacy / ".zattrs").stat().st_mtime_ns
    os.utime(svs_path, ns=(zattrs_ns - 10**9, zattrs_ns - 10**9))
    assert ensure_cache(svs_path) == legacy

    # Overwrite svs_path with an identical-dimension scan at a newer timestamp!
    img2 = np.full((256, 256, 3), 210, dtype=np.uint8)
    tifffile.imwrite(
        svs_path,
        img2,
        tile=(128, 128),
        photometric="rgb",
        compression="jpeg2000",
        compressionargs={"reversible": True},
        description="Aperio Image|MPP = 0.5|AppMag = 20",
    )
    os.utime(svs_path, ns=(zattrs_ns + 2 * 10**9, zattrs_ns + 2 * 10**9))

    assert _legacy_store(svs_path.resolve()) is None
    fresh = ensure_cache(svs_path)
    assert fresh == cache_container(svs_path.resolve())
    assert legacy.is_dir()  # legacy directory preserved untouched, not deleted or quarantined
    assert not list((tmp_path / "pyramids").glob("*.corrupt-*"))
    r_fresh, _ = open_store(fresh)
    try:
        assert int(r_fresh["0"][0, 0, 0]) == 210
    finally:
        r_fresh.close()


# ---------------------------------------------------------------------------
# A4: '#' and '?' path safety (Zarr v2 URL parsing & directory creation)
# ---------------------------------------------------------------------------


def test_a4_hash_and_question_mark_paths_never_delete_parent_folders(tmp_path: Path):
    pytest.importorskip("limnd2")
    pytest.importorskip("nd2")

    # NTFS rejects '?' as a filename character, so exercise '?' alongside '#' on POSIX and '#' on Windows.
    q = "#" if os.name == "nt" else "?"

    backend = ZarrV2Storage()
    parent_hash = tmp_path / "Project" / "#12"
    child_dir = parent_hash / "child"
    child_dir.mkdir(parents=True)
    parent_sentinel = parent_hash / "keep_parent.txt"
    parent_sentinel.write_text("do not delete parent", encoding="utf-8")

    group_path = child_dir / f"x{q}v=1.zarr"
    backend.create_group(group_path)
    assert (group_path / ".zgroup").is_file()
    assert parent_sentinel.read_text(encoding="utf-8") == "do not delete parent"

    # Re-opening an existing non-empty folder via create_group must fail closed ("w-").
    non_empty = child_dir / "existing.ome.zarr"
    non_empty.mkdir()
    keep_file = non_empty / "important.txt"
    keep_file.write_text("keep", encoding="utf-8")
    with pytest.raises(Exception):
        backend.create_group(non_empty)
    assert keep_file.read_text(encoding="utf-8") == "keep"

    slide_parent = tmp_path / "scan#2"
    slide_dir = slide_parent / f"sub{q}q"
    slide_dir.mkdir(parents=True)
    scan_sentinel = slide_parent / "scan_sentinel.txt"
    sub_sentinel = slide_dir / "folder_sentinel.txt"
    scan_sentinel.write_text("scan-intact", encoding="utf-8")
    sub_sentinel.write_text("sub-intact", encoding="utf-8")

    arr = np.ones((64, 64, 1), dtype=np.uint16) * 500
    nd2_path = write_synthetic_nd2(
        slide_dir / f"slide#1{q}a.nd2",
        arr,
        [("DAPI", "blue")],
        pixel_um=0.5,
    )
    cached = ensure_cache(nd2_path)
    r_cached, attrs_cached = open_store(cached)
    r_cached.close()
    assert attrs_cached["nd2wsi"]["levels"][0]["height"] == 64

    reg = SlideRegistry()
    try:
        sid = reg.open_path(nd2_path)
        assert reg.slides[sid].root["0"].shape == (1, 64, 64)
    finally:
        reg.close_all(immediate=True)

    out_zarr = slide_dir / f"out#1{q}v=2.ome.zarr"
    out_svs = slide_dir / f"out#1{q}v=2.nd2svs"
    convert(nd2_path, out_zarr, progress=False)
    convert(nd2_path, out_svs, progress=False)
    for out in (out_zarr, out_svs):
        r_out, _ = open_store(out)
        close_out = getattr(r_out, "close", None)
        if close_out is not None:
            close_out()

    assert scan_sentinel.read_text(encoding="utf-8") == "scan-intact"
    assert sub_sentinel.read_text(encoding="utf-8") == "sub-intact"


# ---------------------------------------------------------------------------
# B2: non-direct SVS fallback uses ensure_cache and detects rescans
# ---------------------------------------------------------------------------


def test_b2_non_direct_svs_fallback_uses_ensure_cache_and_detects_rescans(tmp_path: Path):
    svs_path = tmp_path / "rescan.svs"
    img1 = np.full((64, 64, 3), 120, dtype=np.uint8)
    tifffile.imwrite(
        svs_path,
        img1,
        photometric="rgb",
        description="Aperio Image|MPP = 0.5|AppMag = 20",
    )

    # Untiled SVS is non-direct; open_or_convert leaves registry to resolve it on first open.
    assert existing_cache_store(svs_path) is None
    assert open_or_convert(svs_path) == svs_path

    with running_server([svs_path]) as httpd:
        sid1 = next(iter(httpd.registry.slides))
        st1 = httpd.registry.slides[sid1]
        assert st1.trash_path is not None
        assert np.all(np.asarray(st1.root["0"][0]) == 120)
        store1 = Path(st1.store_path)

    # Replace with same dimensions (64x64) and different pixels: cache must miss & rebuild.
    img2 = np.full((64, 64, 3), 42, dtype=np.uint8)
    tifffile.imwrite(
        svs_path,
        img2,
        photometric="rgb",
        description="Aperio Image|MPP = 0.5|AppMag = 20",
    )
    st_info = svs_path.stat()
    os.utime(svs_path, ns=(st_info.st_atime_ns, st_info.st_mtime_ns + 2_000_000_000))

    assert cache_matches(store1, svs_path.resolve(), PlaneSelection()) is False
    assert existing_cache_store(svs_path) is None
    assert open_or_convert(svs_path) == svs_path

    with running_server([svs_path]) as httpd2:
        sid2 = next(iter(httpd2.registry.slides))
        st2 = httpd2.registry.slides[sid2]
        assert np.all(np.asarray(st2.root["0"][0]) == 42)

    # Replace again with different dimensions (96x80) and open via SlideRegistry.open_path.
    img3 = np.full((96, 80, 3), 77, dtype=np.uint8)
    tifffile.imwrite(
        svs_path,
        img3,
        photometric="rgb",
        description="Aperio Image|MPP = 0.5|AppMag = 20",
    )
    st_info3 = svs_path.stat()
    os.utime(svs_path, ns=(st_info3.st_atime_ns, st_info3.st_mtime_ns + 4_000_000_000))

    reg = SlideRegistry()
    try:
        sid3 = reg.open_path(svs_path)
        st3 = reg.slides[sid3]
        assert tuple(st3.root["0"].shape) == (3, 96, 80)
        assert np.all(np.asarray(st3.root["0"][0]) == 77)
    finally:
        reg.close_all(immediate=True)


# ---------------------------------------------------------------------------
# B6: convert(.nd2svs) cross-drive relpath & symlinked parent vs output symlink
# ---------------------------------------------------------------------------


def test_b6_convert_nd2svs_cross_drive_relpath_and_symlinked_output(
    tmp_path: Path, monkeypatch, symlink_support
):
    pytest.importorskip("limnd2")
    pytest.importorskip("nd2")

    cross_dir = tmp_path / "cross_drive"
    cross_dir.mkdir()
    arr = np.full((64, 64, 1), 321, dtype=np.uint16)
    src = write_synthetic_nd2(
        cross_dir / "cross.nd2",
        arr,
        [("DAPI", "blue")],
        pixel_um=0.5,
    )

    real_relpath = os.path.relpath

    def fake_relpath(path, start=os.curdir):
        if "cross_drive" in str(path) or "cross_drive" in str(start):
            raise ValueError("path is on mount 'D:', start on mount 'C:'")
        return real_relpath(path, start)

    monkeypatch.setattr(os.path, "relpath", fake_relpath)
    cross_out = cross_dir / "portable.nd2svs"
    convert(src, cross_out, progress=False)
    monkeypatch.undo()

    assert cross_out.is_file()
    m_cross = read_manifest(cross_out)
    assert m_cross is not None
    assert m_cross["source"]["relative_path"] == ""
    r_cross, _ = open_store(cross_out)
    r_cross.close()

    reg_cross = SlideRegistry()
    try:
        sid_cross = reg_cross.add_store(cross_out)
        assert reg_cross.slides[sid_cross].source_path is None
    finally:
        reg_cross.close_all(immediate=True)

    # Symlinked output directory reopens with its source; symlinked .nd2svs output file is refused.
    real_out_dir = tmp_path / "real_out_dir"
    real_out_dir.mkdir()
    src_sym = write_synthetic_nd2(
        real_out_dir / "in_real.nd2",
        arr,
        [("DAPI", "blue")],
        pixel_um=0.5,
    )
    linked_parent = tmp_path / "deep" / "nested"
    linked_parent.mkdir(parents=True)
    linked_dir = linked_parent / "linked_out_dir"
    linked_dir.symlink_to(real_out_dir, target_is_directory=True)

    linked_target = linked_dir / "inside_symdir.nd2svs"
    convert(src_sym, linked_target, progress=False)
    real_target = real_out_dir / "inside_symdir.nd2svs"
    assert real_target.is_file()

    reg_sym = SlideRegistry()
    try:
        sid_sym = reg_sym.add_store(real_target)
        assert reg_sym.slides[sid_sym].source_path == src_sym.resolve()
    finally:
        reg_sym.close_all(immediate=True)

    sym_file = tmp_path / "direct_symlink.nd2svs"
    sym_file.symlink_to(real_target)
    with pytest.raises(ValueError, match="symbolic link"):
        convert(src, sym_file, overwrite=True, progress=False)


# ---------------------------------------------------------------------------
# C1: exported .nd2svs cannot be trashed or rewritten in-place by shading
# ---------------------------------------------------------------------------


def test_c1_exported_nd2svs_cannot_be_trashed_or_rewritten_in_place_by_shading(tmp_path: Path):
    pytest.importorskip("limnd2")
    pytest.importorskip("nd2")

    arr = np.full((64, 64, 1), 1500, dtype=np.uint16)
    src = write_synthetic_nd2(
        tmp_path / "bf_scalar.nd2",
        arr,
        [("Brightfield", "white")],
        pixel_um=0.5,
    )
    pyramids_dir = tmp_path / "pyramids"
    pyramids_dir.mkdir()
    exported = pyramids_dir / "exported_copy.nd2svs"
    convert(src, exported, progress=False)

    manifest = read_manifest(exported)
    assert manifest is not None
    assert manifest.get("origin") == "export"

    with running_server([exported]) as httpd:
        sid = next(iter(httpd.registry.slides))
        st = httpd.registry.slides[sid]
        assert st.trash_path is None
        assert st.managed_cache is False
        with urllib.request.urlopen(f"{server_url(httpd)}s/{sid}/api/inspect") as resp:
            assert resp.status == 200
            inspect_body = json.loads(resp.read().decode("utf-8"))
        assert inspect_body["trashable"] is False
        assert "managed .nd2svs cache" in _basic_reason(st)

        with pytest.raises(ValueError, match="exported file"):
            PreparedReplacement(st, event=object())

        # Even if caller bypassed managed_cache flag, origin=="export" in manifest still refuses.
        st.managed_cache = True
        try:
            with pytest.raises(ValueError, match="exported file"):
                PreparedReplacement(st, event=object())
        finally:
            st.managed_cache = False

        req = urllib.request.Request(
            f"{server_url(httpd)}api/trash",
            data=json.dumps({"sid": sid}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
        body = json.loads(exc_info.value.read().decode("utf-8"))
        assert "no cache managed" in body["error"]

    assert exported.is_file()


# ---------------------------------------------------------------------------
# C2: overwriting 2.6.2 .zgroup .nd2svs/ preserves old dir if build/publish fails
# ---------------------------------------------------------------------------


def test_c2_overwrite_262_zgroup_dir_keeps_old_store_if_publish_fails(
    tmp_path: Path, monkeypatch
):
    pytest.importorskip("limnd2")
    pytest.importorskip("nd2")

    arr = np.arange(64 * 80, dtype=np.uint16).reshape(64, 80, 1)
    src = write_synthetic_nd2(
        tmp_path / "sample_c2.nd2",
        arr,
        [("DAPI", "blue")],
        pixel_um=0.5,
    )
    legacy_dir = tmp_path / "legacy_262.nd2svs"
    legacy_dir.mkdir()
    (legacy_dir / ".zgroup").write_text('{"zarr_format": 2}', encoding="utf-8")
    sentinel = legacy_dir / "old_level0_payload.bin"
    sentinel.write_bytes(b"old-262-store-bytes")

    def boom(*args, **kwargs):
        raise RuntimeError("injected downsample failure")

    monkeypatch.setattr(convert_mod, "_downsample_into", boom)
    with pytest.raises(RuntimeError, match="injected downsample failure"):
        convert(src, legacy_dir, tile=16, overwrite=True, progress=False)

    assert legacy_dir.is_dir()
    assert (legacy_dir / ".zgroup").is_file()
    assert sentinel.read_bytes() == b"old-262-store-bytes"
    assert not list(tmp_path.glob(".legacy_262.nd2svs.*"))


# ---------------------------------------------------------------------------
# CPE-4: cache_removal (nd2wsi-cache/5 + plate .corrupt) & migration
# ---------------------------------------------------------------------------


def test_cpe4_cache_removal_and_migration_accept_corrected_v5_and_corrupt_plate_chunks(
    tmp_path: Path,
):
    pytest.importorskip("limnd2")
    pytest.importorskip("nd2")

    # (a) Corrected single-file pyramid cache (nd2wsi-cache/5 + h-correction/*)
    src = write_synthetic_nd2(
        tmp_path / "slide.nd2",
        np.ones((64, 64, 3), dtype=np.uint8) * 180,
        [("Brightfield", "white")],
        pixel_um=0.5,
    )
    container = ensure_cache(src)
    manifest = read_manifest(container)
    assert manifest is not None
    manifest["format"] = CORRECTED_SINGLE_FILE_FORMAT
    write_entry(container, MANIFEST_NAME, json.dumps(manifest).encode("utf-8"))
    write_entry(container, "h-correction/model.npz", b"NPZ-PAYLOAD")
    write_entry(container, "h-correction/provenance.json", b'{"version": 1}')

    _validate_file_cache_for_trash(container, manifest)

    # Unknown embedded key must still fail closed!
    write_entry(container, "h-correction/user_notes.txt", b"do not delete")
    with pytest.raises(ValueError, match="unknown or user-owned"):
        _validate_file_cache_for_trash(container, manifest)

    # (b) Single-file plate cache with quarantined .corrupt-<32hex> chunk
    czi_plate = _build_czi_custom(
        tmp_path / "stack.czi",
        [
            {"data": np.full((64, 64), 100, dtype=np.uint16), "z": 0},
            {"data": np.full((64, 64), 200, dtype=np.uint16), "z": 1},
        ],
    )
    ps = PlateSource(czi_plate)
    try:
        ps.reduced(0, 0, 0, THUMB_K)
        assert ps.store is not None
        assert ps.store._quarantine_shared_payload(0, 0) is True
        plate_path = ps.store.container
        plate_manifest = dict(ps.store.manifest)
    finally:
        ps.close()
    _validate_file_cache_for_trash(plate_path, plate_manifest)

    # (c) Legacy directory plate cache with quarantined .corrupt-<timestamp> chunk imported via migration
    legacy_plate_dir = tmp_path / "legacy--plate.nd2wsi-cache"
    ps_no_store = PlateSource(czi_plate, store=False)
    try:
        shape = PlateStore._shape(ps_no_store)
        PlateStore._create(ps_no_store, legacy_plate_dir, quick_fingerprint(czi_plate), shape)
    finally:
        ps_no_store.close()
    corrupt_chunk = (
        legacy_plate_dir / "thumbs.zarr" / "thumbs" / "0.0.0.0.0.0.corrupt-20261114T120000-a1b2c3d4"
    )
    corrupt_chunk.write_bytes(b"corrupt-bytes")
    keys = [k for k, _ in cache_entries(legacy_plate_dir)]
    assert "thumbs.zarr/thumbs/0.0.0.0.0.0.corrupt-20261114T120000-a1b2c3d4" in keys
    packed_plate = tmp_path / "migrated--plate.nd2svs"
    pack_directory(legacy_plate_dir, packed_plate, source=czi_plate)
    packed_manifest = read_manifest(packed_plate)
    assert packed_manifest is not None
    _validate_file_cache_for_trash(packed_plate, packed_manifest)


# ---------------------------------------------------------------------------
# CPE-5: convert(..., "out.nd2svs") & cli info on .nd2svs / .ome.zarr
# ---------------------------------------------------------------------------


def test_cpe5_convert_nd2svs_single_file_and_cli_info(tmp_path: Path, capsys):
    pytest.importorskip("limnd2")
    pytest.importorskip("nd2")

    arr = np.arange(64 * 80, dtype=np.uint16).reshape(64, 80, 1)
    src = write_synthetic_nd2(tmp_path / "sample.nd2", arr, [("DAPI", "blue")], pixel_um=0.5)

    out_nd2svs = tmp_path / "portable.nd2svs"
    convert(src, out_nd2svs, progress=False)
    assert out_nd2svs.is_file()
    assert is_single_file(out_nd2svs)
    m = read_manifest(out_nd2svs)
    assert m is not None and m["format"] == SINGLE_FILE_FORMAT and m["kind"] == "full"

    root, attrs = open_store(out_nd2svs)
    try:
        assert np.array_equal(np.asarray(root["0"][0]), arr[:, :, 0])
    finally:
        root.close()

    with pytest.raises(FileExistsError):
        convert(src, out_nd2svs, progress=False)

    # Overwriting a broken 2.6.2 directory named *.nd2svs/ succeeds if it is a Zarr store
    out_nd2svs.unlink()
    out_nd2svs.mkdir()
    (out_nd2svs / ".zgroup").write_text('{"zarr_format": 2}', encoding="utf-8")
    convert(src, out_nd2svs, overwrite=True, progress=False)
    assert out_nd2svs.is_file() and is_single_file(out_nd2svs)

    # Refuses to delete a non-Zarr user directory even with overwrite=True!
    user_dir = tmp_path / "important_folder.nd2svs"
    user_dir.mkdir()
    note = user_dir / "notes.txt"
    note.write_text("precious user notes", encoding="utf-8")
    with pytest.raises(IsADirectoryError):
        convert(src, user_dir, overwrite=True, progress=False)
    assert note.read_text(encoding="utf-8") == "precious user notes"

    # CLI info on .nd2svs (human-readable & --json) and .ome.zarr
    capsys.readouterr()
    assert cli_main(["info", str(out_nd2svs)]) == 0
    text_out = capsys.readouterr().out
    assert "sample.nd2" in text_out
    assert "DAPI" in text_out

    assert cli_main(["info", "--json", str(out_nd2svs)]) == 0
    json_info = json.loads(capsys.readouterr().out)
    assert json_info["source"] == "sample.nd2"
    assert json_info["dtype"] == "uint16"
    assert json_info["pixel_size_um"] == pytest.approx([0.5, 0.5])
    assert json_info["channels"][0]["label"] == "DAPI"
    assert len(json_info["levels"]) >= 1

    ome_dir = tmp_path / "portable.ome.zarr"
    convert(src, ome_dir, progress=False)
    assert cli_main(["info", "--json", str(ome_dir)]) == 0
    ome_info = json.loads(capsys.readouterr().out)
    assert ome_info["source"] == "sample.nd2"
    assert ome_info["tile"] == json_info["tile"]


# ---------------------------------------------------------------------------
# CPE-6: tile parameter respected in cache_matches, existing_cache_store, ensure_cache, cli
# ---------------------------------------------------------------------------


def test_cpe6_tile_mismatch_invalidates_and_rebuilds_cache(tmp_path: Path, monkeypatch):
    svs_path = tmp_path / "tiled.svs"
    img = np.ones((600, 600, 3), dtype=np.uint8) * 123
    tifffile.imwrite(
        svs_path,
        img,
        tile=(240, 240),
        photometric="rgb",
        compression="jpeg2000",
        compressionargs={"reversible": True},
        description="Aperio Image|MPP = 0.5|AppMag = 20",
    )

    store_256 = ensure_cache(svs_path, tile=256)
    assert cache_matches(store_256, svs_path.resolve(), PlaneSelection(), tile=256) is True
    assert cache_matches(store_256, svs_path.resolve(), PlaneSelection(), tile=512) is False
    assert existing_cache_store(svs_path, tile=256) == store_256
    assert existing_cache_store(svs_path, tile=512) is None

    store_512 = ensure_cache(svs_path, tile=512)
    assert store_512 == store_256
    r512, attrs512 = open_store(store_512)
    r512.close()
    assert attrs512["nd2wsi"]["tile"] == 512
    assert list(store_512.parent.glob(f"{store_512.name}.corrupt-*"))

    # C7: CLI view --tile 256 when default_store_path has tile=512 and managed cache has tile=512
    # must fail with exit code 1 rather than silently serving the tile=512 store;
    # --store PATH redirects to a 256-tile store and --overwrite replaces default_store_path.
    assert CORRECTED_SINGLE_FILE_FORMAT == "nd2wsi-cache/5"
    from nd2wsi.convert import default_store_path

    default_512 = default_store_path(svs_path)
    convert(svs_path, default_512, tile=512, progress=False)
    confirm_calls: list[Path] = []
    served_stores: list[Path] = []
    monkeypatch.setattr(
        "nd2wsi.cli.confirm_pyramid",
        lambda s, assume_yes=False: confirm_calls.append(Path(s)) or True,
    )
    monkeypatch.setattr("nd2wsi.server.serve", lambda stores, **kw: served_stores.extend(stores))

    assert cli_main(["view", str(svs_path), "--tile", "256"]) == 1
    assert confirm_calls == []
    assert served_stores == []
    assert _store_attrs(store_512)["nd2wsi"]["tile"] == 512
    assert _store_attrs(default_512)["nd2wsi"]["tile"] == 512

    alt_256 = tmp_path / "alt_tile256.nd2svs"
    assert cli_main(["view", str(svs_path), "--tile", "256", "--store", str(alt_256)]) == 0
    assert len(confirm_calls) == 1
    assert served_stores == [alt_256]
    assert _store_attrs(alt_256)["nd2wsi"]["tile"] == 256
    assert _store_attrs(default_512)["nd2wsi"]["tile"] == 512

    confirm_calls.clear()
    served_stores.clear()
    assert cli_main(["view", str(svs_path), "--tile", "256", "--overwrite"]) == 0
    assert len(confirm_calls) == 1
    assert served_stores == [default_512]
    assert _store_attrs(default_512)["nd2wsi"]["tile"] == 256


# ---------------------------------------------------------------------------
# CPE-7a & CPE-7b: plate legacy fallback & RGB CZI :zero-bg tag
# ---------------------------------------------------------------------------


def test_cpe7a_stale_or_corrupt_legacy_plate_dir_preserved_and_falls_back_to_nd2svs(
    tmp_path: Path,
):
    czi_plate = _build_czi_custom(
        tmp_path / "plate_test.czi",
        [
            {"data": np.full((64, 64), 110, dtype=np.uint16), "z": 0},
            {"data": np.full((64, 64), 220, dtype=np.uint16), "z": 1},
        ],
    )
    default_pkg = plate_container(czi_plate)
    legacy_dir = default_pkg.with_name(default_pkg.name[: -len(".nd2svs")] + ".nd2wsi-cache")

    # Build a legacy directory store, then make its source fingerprint stale.
    ps0 = PlateSource(czi_plate, store=False)
    try:
        shape = PlateStore._shape(ps0)
        PlateStore._create(ps0, legacy_dir, quick_fingerprint(czi_plate), shape)
    finally:
        ps0.close()
    m_path = legacy_dir / MANIFEST_NAME
    stale_m = json.loads(m_path.read_text(encoding="utf-8"))
    stale_m["source"]["quick_sha256"] = "0" * 64
    m_path.write_text(json.dumps(stale_m), encoding="utf-8")

    before_tree = {
        str(p.relative_to(legacy_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in legacy_dir.rglob("*")
        if p.is_file()
    }

    # Implicit PlateSource open must NOT fail with store=None; it must preserve
    # legacy_dir untouched and create default_pkg (.nd2svs)!
    ps1 = PlateSource(czi_plate)
    try:
        assert ps1.store is not None and ps1.store.writable
        assert ps1.store.container == default_pkg
        assert default_pkg.is_file()
    finally:
        ps1.close()

    after_tree = {
        str(p.relative_to(legacy_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in legacy_dir.rglob("*")
        if p.is_file()
    }
    assert after_tree == before_tree

    # Newer-format legacy manifest must still raise RuntimeError from PlateStore.open_or_create!
    default_pkg.unlink()
    newer_m = dict(stale_m, format="nd2wsi-plate/99")
    m_path.write_text(json.dumps(newer_m), encoding="utf-8")
    ps_check = PlateSource(czi_plate, store=False)
    try:
        with pytest.raises(RuntimeError, match="newer format"):
            PlateStore.open_or_create(ps_check)
    finally:
        ps_check.close()


def test_cpe7b_rgb_czi_partial_plane_zero_bg_tag_invalidates_261_plate_cache(tmp_path: Path):
    full_z0 = np.full((64, 64, 3), 120, dtype=np.uint8)
    part_z1 = np.full((32, 32, 3), 200, dtype=np.uint8)
    czi_path = _build_czi_custom(
        tmp_path / "rgb_partial.czi",
        [
            {"data": full_z0, "z": 0, "x": 0, "y": 0, "pixel_type": 3},  # Bgr24
            {"data": part_z1, "z": 1, "x": 0, "y": 0, "pixel_type": 3},  # partial 32x32 on 64x64
        ],
        channels=[("Brightfield", "#FFFFFFFF")],
    )

    ps = PlateSource(czi_path)
    try:
        assert ps._channel_dtypes == ["uint8"] * 3
        assert ps.store is not None
        assert ps.store.manifest["thumbs"]["channel_dtypes"] == ["uint8"] * 3
        red_z1 = ps.reduced(0, 0, 1, THUMB_K)
        # Uncovered bottom-right quadrant must be zero-padded (0), not white-padded (255)!
        assert np.all(red_z1[:, 4:, 4:] == 0)
        assert np.all(red_z1[:, :4, :4] == 200)
        pkg = ps.store.container
    finally:
        ps.close()

    # Valid 2.6.2 plate manifest with ["uint8"] * 3 must be reused without quarantine.
    ps_reuse = PlateSource(czi_path)
    try:
        assert ps_reuse.store is not None
        assert ps_reuse.store.manifest["thumbs"]["channel_dtypes"] == ["uint8"] * 3
    finally:
        ps_reuse.close()
    assert not list(pkg.parent.glob(f"{pkg.name}.corrupt-*"))

    # Forge a 2.6.1 plate manifest lacking channel_dtypes: must invalidate and rebuild.
    old_m = read_manifest(pkg)
    assert old_m is not None
    old_m["thumbs"].pop("channel_dtypes", None)
    write_entry(pkg, MANIFEST_NAME, json.dumps(old_m).encode("utf-8"))

    ps2 = PlateSource(czi_path)
    try:
        assert ps2.store is not None
        assert ps2.store.manifest["thumbs"]["channel_dtypes"] == ["uint8"] * 3
    finally:
        ps2.close()
    assert list(pkg.parent.glob(f"{pkg.name}.corrupt-*"))


# ---------------------------------------------------------------------------
# CPE-8: ZarrV2ChunkArray with compressor=None
# ---------------------------------------------------------------------------


def test_cpe8_zarr_v2_chunk_array_uncompressed_read_and_write(tmp_path: Path):
    from nd2wsi.convert import _downsample_into

    group_path = tmp_path / "uncompressed.zarr"
    root = zarr.open_group(str(group_path), mode="w", zarr_format=2)
    arr0 = root.create_array(
        name="0",
        shape=(1, 20, 20),
        chunks=(1, 16, 16),
        dtype=np.uint16,
        compressors=None,
        fill_value=0,
    )
    arr1 = root.create_array(
        name="1",
        shape=(1, 10, 10),
        chunks=(1, 16, 16),
        dtype=np.uint16,
        compressors=None,
        fill_value=0,
    )
    assert json.loads((group_path / "0" / ".zarray").read_text())["compressor"] is None

    ca0 = ZarrV2ChunkArray(arr0)
    assert ca0._codec is None
    assert ca0.read_or_none((0, 0, 0)) is None

    block = np.arange(1, 1 + 16 * 16, dtype=np.uint16).reshape(1, 16, 16)
    assert ca0.write((0, 0, 0), block) is True
    assert np.array_equal(ca0.read((0, 0, 0)), block)
    assert np.array_equal(np.asarray(arr0[:, :16, :16]), block)

    # Edge block auto-pads and zero block unlinks
    edge = np.full((1, 4, 4), 400, dtype=np.uint16)
    assert ca0.write((0, 1, 1), edge) is True
    assert np.array_equal(np.asarray(arr0[:, 16:20, 16:20]), edge)

    _downsample_into(arr0, arr1, 16, np.dtype(np.uint16))
    ca1 = ZarrV2ChunkArray(arr1)
    got1 = ca1.read((0, 0, 0))[:, :10, :10]
    assert int(got1[0, 8, 8]) == 400
    assert ca0.write((0, 1, 1), np.zeros((1, 16, 16), dtype=np.uint16)) is False
    assert ca0.read_or_none((0, 1, 1)) is None

    # MINOR2-F2: ZarrV2ChunkArray must reject arrays with truthy filters metadata.
    filt_dir = tmp_path / "filtered_array.zarr"
    filt_dir.mkdir()
    (filt_dir / ".zarray").write_text(
        json.dumps(
            {
                "chunks": [1, 16, 16],
                "compressor": None,
                "dtype": "<u2",
                "fill_value": 0,
                "filters": [{"id": "shuffle", "elementsize": 2}],
                "order": "C",
                "shape": [1, 16, 16],
                "zarr_format": 2,
            }
        ),
        encoding="utf-8",
    )
    arr_filtered = zarr.open_array(str(filt_dir), mode="r")
    with pytest.raises(ValueError, match="filters"):
        ZarrV2ChunkArray(arr_filtered)
