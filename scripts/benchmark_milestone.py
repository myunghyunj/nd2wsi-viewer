#!/usr/bin/env python3
"""Milestone 1 Empirical Performance, Multi-GB Scale & Memory Gate Harness.

Supports four execution modes:
- ``--suite smoke``: Fast (< 5 s) synthetic round-trip + 4-tab RSS gate check for CI/tests.
- ``--suite standard``: Standard Milestone 1 multi-vendor suite (isolated copies of
  ``docs/example_*.nd2`` when present + multi-scene ``.czi``, ``.lif``, ``.ome.tif``,
  ``72 MiB`` untiled ``.ome.tif``, and ``Z=22`` stacks).
- ``--suite scale``: Multi-GB scale stress suite in isolated ``$TMPDIR`` with 100% cleanup:
  * ``16384 x 16384`` 3-ch ``uint16`` (``1.50 GiB`` raw) 5-level tiled pyramidal ``.ome.tif``
  * ``16384 x 12288`` 4-ch ``uint16`` (``1.50 GiB`` raw) untiled ``.ome.tif`` (``_LazyUntiledLevel``)
  * ``12288 x 12288`` 3-ch ``uint16`` (``864 MiB`` raw) ``6x6`` Zstd ``.czi`` mosaic (``.nd2svs`` build/open)
  * ``2048 x 2048`` 3-ch ``uint16`` ``Z=32`` (``768 MiB`` raw) Zstd ``.czi`` Z-stack rapid scrub
  * 4-concurrent-large-slide Compare burst (``4.59 GiB`` combined raw) gated against
    ``--max-4tab-rss-mib 1228.8`` (``1.2 GiB``).
- ``--files <path1> [path2 ...]``: Benchmarks external acquisitions inside an isolated
  temp sandbox without creating, modifying, or deleting ``nd2wsi/`` beside source files.

Isolation guarantees:
- Never launches the native GUI viewer.
- Never writes to ``~/Library/Application Support/nd2wsi-viewer`` or beside user slides.
- Runs every measurement in a dedicated child Python subprocess for clean ``ru_maxrss``
  and live ``ps`` RSS accounting, deleting 100% of temporary directories in ``finally``.
"""

from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
import platform
import resource
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import imagecodecs
import numpy as np
import tifffile

DEFAULT_MAX_4TAB_RSS_MIB = 1228.8  # 1.2 GiB acceptance ceiling (1.2 * 1024 MiB)
EXIT_GATE_FAILED = 2


# ---------------------------------------------------------------------------
# Statistics, Process RSS & Safe File Staging Helpers
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    arr = np.asarray(sorted(values), dtype=np.float64)
    return float(np.percentile(arr, pct))


def summarize_ms(samples_ms: list[float]) -> dict[str, Any]:
    if not samples_ms:
        return {"n": 0, "median_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None}
    return {
        "n": len(samples_ms),
        "median_ms": round(float(statistics.median(samples_ms)), 2),
        "p95_ms": round(_percentile(samples_ms, 95.0), 2),
        "mean_ms": round(float(statistics.mean(samples_ms)), 2),
        "min_ms": round(float(min(samples_ms)), 2),
        "max_ms": round(float(max(samples_ms)), 2),
        "samples_ms": [round(float(x), 2) for x in samples_ms],
    }


def summarize_mib(samples_mib: list[float]) -> dict[str, Any]:
    if not samples_mib:
        return {"n": 0, "median_mib": None, "p95_mib": None, "max_mib": None}
    return {
        "n": len(samples_mib),
        "median_mib": round(float(statistics.median(samples_mib)), 1),
        "p95_mib": round(_percentile(samples_mib, 95.0), 1),
        "max_mib": round(float(max(samples_mib)), 1),
        "samples_mib": [round(float(x), 1) for x in samples_mib],
    }


def current_ru_maxrss_mib() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return float(ru) / (1024.0 * 1024.0)
    return float(ru) / 1024.0


def _sample_ps_rss_mib(pid: int) -> float:
    if os.name == "nt":
        return 0.0
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return float(int(out)) / 1024.0
    except Exception:
        pass
    return 0.0


class RssSampler:
    """Samples live process RSS via ``ps`` alongside kernel ``ru_maxrss``."""

    def __init__(self, interval_s: float = 0.025) -> None:
        self.pid = os.getpid()
        self.interval_s = interval_s
        self.peak_ps_mib = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="rss-sampler", daemon=True)

    def _sample_once(self) -> None:
        mib = _sample_ps_rss_mib(self.pid)
        if mib > self.peak_ps_mib:
            self.peak_ps_mib = mib

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(self.interval_s)

    def __enter__(self) -> RssSampler:
        self._sample_once()
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._sample_once()

    def report(self) -> dict[str, float]:
        ru_mib = current_ru_maxrss_mib()
        return {
            "ru_maxrss_mib": round(ru_mib, 1),
            "ps_peak_rss_mib": round(self.peak_ps_mib, 1),
            "peak_rss_mib": round(max(ru_mib, self.peak_ps_mib), 1),
        }


def _apfs_clonefile(src: Path, dst: Path) -> bool:
    """Attempt zero-copy APFS copy-on-write clone on macOS (independent inode, zero source mutation)."""
    if sys.platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = getattr(libc, "clonefile", None)
        if fn is None:
            return False
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        fn.restype = ctypes.c_int
        rc = fn(os.fsencode(str(src)), os.fsencode(str(dst)), 0)
        return rc == 0 and dst.is_file()
    except Exception:
        return False


def _clone_or_copy_file(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> str:
    """Zero-copy APFS clonefile on same-volume macOS sandboxes, falling back to shutil.copy2."""
    src_p, dst_p = Path(src), Path(dst)
    if not _apfs_clonefile(src_p, dst_p):
        shutil.copy2(src_p, dst_p)
    return str(dst_p)


def stage_isolated_slide(src: Path, sandbox_dir: Path, *, dst_name: str | None = None) -> Path:
    """Stage ``src`` as an independent regular file inside ``sandbox_dir``.

    Never creates symbolic links (which ``Path.resolve()`` in ``nd2wsi.server`` would
    resolve back to ``src.parent``, causing ``nd2wsi/`` caches to be written beside
    a user's real slide). Uses APFS ``clonefile`` when on the same volume, falling
    back to ``shutil.copy2``.
    """
    src_resolved = src.resolve()
    sandbox_resolved = sandbox_dir.resolve()
    if sandbox_resolved == src_resolved.parent:
        raise ValueError(f"Refusing to stage slide inside its own source directory: {sandbox_resolved}")
    sandbox_resolved.mkdir(parents=True, exist_ok=True)
    dst = sandbox_resolved / (dst_name or src_resolved.name)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    _clone_or_copy_file(src_resolved, dst)
    if dst.is_symlink() or dst.resolve().parent != sandbox_resolved:
        raise RuntimeError(f"Sandbox isolation check failed for staged file {dst}")
    return dst


def _dir_snapshot(folder: Path) -> dict[str, tuple[int, int, int]]:
    """Snapshot ``{entry_name: (is_dir, size_or_zero, mtime_ns)}`` of a source directory for non-mutation checks."""
    snap: dict[str, tuple[int, int, int]] = {}
    for item in sorted(folder.iterdir(), key=lambda p: p.name):
        try:
            st = item.lstat()
            is_d = int(item.is_dir())
            sz = 0 if is_d else int(st.st_size)
            mt = int(st.st_mtime_ns)
        except OSError:
            is_d, sz, mt = 0, -1, -1
        snap[item.name] = (is_d, sz, mt)
    return snap


# ---------------------------------------------------------------------------
# Synthetic Microscopy Container Builders (CZI, LIF, OME-TIFF)
# ---------------------------------------------------------------------------


def _make_fluorescence_plane(
    rng: np.random.Generator, h: int, w: int, ch_seed: int
) -> np.ndarray:
    """Generate a structured uint16 fluorescence plane with non-trivial entropy."""
    yy = np.linspace(0.0, np.pi * (3 + ch_seed), h, dtype=np.float32)[:, None]
    xx = np.linspace(0.0, np.pi * (4 + ch_seed), w, dtype=np.float32)[None, :]
    wave = (np.sin(yy) * np.cos(xx) + 1.0) * 900.0
    sh = max(1, h // 4)
    sw = max(1, w // 4)
    spots = rng.integers(120, 900, size=(sh, sw), dtype=np.uint16)
    spots_up = np.repeat(np.repeat(spots, 4, axis=0), 4, axis=1)[:h, :w]
    noise = rng.integers(0, 260, size=(h, w), dtype=np.uint16)
    plane = np.clip(wave.astype(np.uint32) + spots_up + noise, 0, 65535).astype(np.uint16)
    ly0, ly1 = min(h // 4, h), min(h // 2, h)
    lx0, lx1 = min(w // 4, w), min(w // 2, w)
    if ly1 > ly0 and lx1 > lx0:
        plane[ly0:ly1, lx0:lx1] = np.clip(
            plane[ly0:ly1, lx0:lx1].astype(np.uint32) + 6000 + ch_seed * 1500, 0, 65535
        ).astype(np.uint16)
    return plane


def build_synthetic_czi(
    path: Path,
    planes: dict[tuple[int, ...], np.ndarray | bytes],
    *,
    tile_shape: tuple[int, int] | None = None,
    pixel_type: int = 1,  # Gray16
    compression: int = 5,  # Zstd0
    pixel_size_um: tuple[float, float] = (0.25, 0.25),
    z_step_um: float | None = None,
    magnification: float = 40.0,
    channels: list[tuple[str, str]] | None = None,
) -> Path:
    """Write a calibrated Zeiss CZI container.

    Keys in ``planes`` may be:
    - ``(z, c, m)`` -> ``s=0, xs=0, ys=0``
    - ``(z, c, m, xs, ys)`` -> ``s=0`` mosaic SubBlock at ``(xs, ys)``
    - ``(s, z, c, m, xs, ys)`` -> multi-scene / mosaic SubBlock

    Values may be ``uint16`` 2D ``np.ndarray`` or pre-compressed Zstd ``bytes``
    (when ``tile_shape=(h, w)`` is supplied for fast multi-hundred-MB streaming).
    """
    dir_records: list[tuple[int, ...]] = []
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 512)
        fpos_cursor = 512

        for key, arr_or_bytes in planes.items():
            if len(key) == 3:
                z, c, m = key
                s, xs, ys = 0, 0, 0
            elif len(key) == 5:
                z, c, m, xs, ys = key
                s = 0
            elif len(key) == 6:
                s, z, c, m, xs, ys = key
            else:
                raise ValueError(f"Unsupported CZI plane key tuple: {key!r}")
            t = 0
            if isinstance(arr_or_bytes, (bytes, bytearray, memoryview)):
                if tile_shape is None:
                    raise ValueError("tile_shape=(h, w) required when passing pre-encoded bytes")
                h, w = tile_shape
                payload = bytes(arr_or_bytes)
            else:
                h, w = arr_or_bytes.shape
                raw_bytes = np.ascontiguousarray(arr_or_bytes).tobytes()
                payload = imagecodecs.zstd_encode(raw_bytes) if compression == 5 else raw_bytes

            fpos = fpos_cursor
            ndims = 7
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
                pixel_type,
                fpos,
                0,
                compression,
                0,
                0,
                b"\x00" * 4,
                ndims,
            )
            dims = [
                (b"X\x00\x00\x00", xs, w, float(xs), w),
                (b"Y\x00\x00\x00", ys, h, float(ys), h),
                (b"C\x00\x00\x00", c, 1, float(c), 1),
                (b"Z\x00\x00\x00", z, 1, float(z), 1),
                (b"M\x00\x00\x00", m, 1, float(m), 1),
                (b"T\x00\x00\x00", t, 1, float(t), 1),
                (b"S\x00\x00\x00", s, 1, float(s), 1),
            ]
            for idx, (dname, st_i, sz_i, st_f, st_sz) in enumerate(dims):
                struct.pack_into("<4siifi", sb_prefix, 48 + idx * 20, dname, st_i, sz_i, st_f, st_sz)

            fh.write(seg_hdr)
            fh.write(sb_prefix)
            fh.write(payload)
            if alloc_size > used_size:
                fh.write(b"\x00" * (alloc_size - used_size))
            fpos_cursor += 32 + alloc_size
            dir_records.append((fpos, xs, w, ys, h, t, z, c, m, s, pixel_type))

        dir_pos = fpos_cursor
        dir_payload = bytearray(128)
        struct.pack_into("<i", dir_payload, 0, len(dir_records))
        for fpos, xs, w, ys, h, t, z, c, m, s, sb_ptype in dir_records:
            dir_payload.extend(
                struct.pack(
                    "<2siqiiBB4si",
                    b"DV",
                    sb_ptype,
                    fpos,
                    0,
                    compression,
                    0,
                    0,
                    b"\x00" * 4,
                    7,
                )
            )
            for dname, st_i, sz_i in (
                (b"X\x00\x00\x00", xs, w),
                (b"Y\x00\x00\x00", ys, h),
                (b"C\x00\x00\x00", c, 1),
                (b"Z\x00\x00\x00", z, 1),
                (b"M\x00\x00\x00", m, 1),
                (b"T\x00\x00\x00", t, 1),
                (b"S\x00\x00\x00", s, 1),
            ):
                dir_payload.extend(struct.pack("<4siifi", dname, st_i, sz_i, float(st_i), sz_i))
        dir_used = len(dir_payload)
        dir_alloc = (dir_used + 31) // 32 * 32
        fh.write(struct.pack("<16sQQ", b"ZISRAWDIRECTORY\x00", dir_alloc, dir_used))
        fh.write(dir_payload)
        if dir_alloc > dir_used:
            fh.write(b"\x00" * (dir_alloc - dir_used))
        fpos_cursor += 32 + dir_alloc

        meta_pos = fpos_cursor
        ch_nodes = ""
        for idx, (cname, ccolor) in enumerate(
            channels or [("DAPI", "#FF00A0FF"), ("EGFP", "#FF00FF33"), ("Cy3", "#FFFF8000")]
        ):
            ch_nodes += (
                f'<Channel Id="Channel:{idx}" Name="{cname}">'
                f"<ShortName>{cname}</ShortName><Color>{ccolor}</Color></Channel>"
            )
        py_m = pixel_size_um[0] * 1e-6
        px_m = pixel_size_um[1] * 1e-6
        z_dist_xml = (
            f'<Distance Id="Z"><Value>{z_step_um * 1e-6}</Value></Distance>'
            if z_step_um is not None
            else ""
        )
        xml_str = (
            '<?xml version="1.0" encoding="utf-8"?>'
            "<ImageDocument><Metadata>"
            "<Information><Instrument><Objectives>"
            f'<Objective Id="Obj:1"><NominalMagnification>{magnification}</NominalMagnification></Objective>'
            "</Objectives></Instrument></Information>"
            "<Scaling><Items>"
            f'<Distance Id="X"><Value>{px_m}</Value></Distance>'
            f'<Distance Id="Y"><Value>{py_m}</Value></Distance>'
            f"{z_dist_xml}"
            "</Items></Scaling>"
            f"<DisplaySetting><Channels>{ch_nodes}</Channels></DisplaySetting>"
            "</Metadata></ImageDocument>"
        )
        xml_bytes = xml_str.encode("utf-8")
        meta_hdr_payload = bytearray(256)
        struct.pack_into("<II", meta_hdr_payload, 0, len(xml_bytes), 0)
        meta_used = 256 + len(xml_bytes)
        meta_alloc = (meta_used + 31) // 32 * 32
        fh.write(struct.pack("<16sQQ", b"ZISRAWMETADATA\x00\x00", meta_alloc, meta_used))
        fh.write(meta_hdr_payload)
        fh.write(xml_bytes)
        if meta_alloc > meta_used:
            fh.write(b"\x00" * (meta_alloc - meta_used))

        file_hdr = bytearray(512)
        struct.pack_into("<16sQQ", file_hdr, 0, b"ZISRAWFILE\x00\x00\x00\x00\x00\x00", 480, 80)
        struct.pack_into(
            "<iiii16s16siQQiQ",
            file_hdr,
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
        fh.seek(0)
        fh.write(file_hdr)
    return path


def build_synthetic_lif(path: Path, series_list: list[dict[str, Any]]) -> Path:
    """Write a calibrated Leica LIF v2 container from ``series_list``."""
    elem_xmls: list[str] = []
    mem_payloads: list[tuple[str, bytes]] = []
    for idx, spec in enumerate(series_list):
        name = spec.get("name", f"Series{idx:03d}")
        mem_id = f"MemBlock_{idx + 1}"
        payload: bytes = spec["payload"]
        dims_xml = "".join(spec["dims_xml"])
        chans_xml = "".join(spec["chans_xml"])
        extra_xml = spec.get("extra_xml", "")
        elem_xmls.append(
            f'<Element Name="{name}">'
            f"<Data><Image>{extra_xml}"
            f"<ImageDescription>"
            f"<Channels>{chans_xml}</Channels>"
            f"<Dimensions>{dims_xml}</Dimensions>"
            f"</ImageDescription>"
            f"</Image></Data>"
            f'<Memory Size="{len(payload)}" MemoryBlockID="{mem_id}"/>'
            f"</Element>"
        )
        mem_payloads.append((mem_id, payload))

    xml_str = (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<LMSDataContainerHeader Version="2">'
        f"{''.join(elem_xmls)}"
        "</LMSDataContainerHeader>"
    )
    xml_utf16 = xml_str.encode("utf-16-le")
    xml_nc = len(xml_utf16) // 2
    with open(path, "wb") as fh:
        fh.write(struct.pack("<iIBI", 0x70, 5 + len(xml_utf16), 0x2A, xml_nc))
        fh.write(xml_utf16)
        for mem_id, payload in mem_payloads:
            id_utf16 = mem_id.encode("utf-16-le")
            id_nc = len(id_utf16) // 2
            chunk_len = 1 + 8 + 1 + 4 + len(id_utf16)
            fh.write(struct.pack("<iIBQBI", 0x70, chunk_len, 0x2A, len(payload), 0x2A, id_nc))
            fh.write(id_utf16)
            fh.write(payload)
    return path


# ---------------------------------------------------------------------------
# Suite Fixture Builders (Smoke, Standard, Scale)
# ---------------------------------------------------------------------------


def prepare_smoke_fixtures(fixtures_dir: Path) -> dict[str, Path]:
    """Generate fast (< 0.2 s) compact fixtures for ``--suite smoke`` CI/unit tests."""
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20261118)
    planes_512 = np.stack([_make_fluorescence_plane(rng, 512, 512, c) for c in range(3)], axis=0)

    ome_2d = fixtures_dir / "smoke_2d.ome.tif"
    with tifffile.TiffWriter(str(ome_2d), bigtiff=False, ome=True) as tw:
        tw.write(
            planes_512,
            photometric="minisblack",
            tile=(256, 256),
            compression="zlib",
            metadata={
                "axes": "CYX",
                "PhysicalSizeX": 0.325,
                "PhysicalSizeXUnit": "µm",
                "PhysicalSizeY": 0.325,
                "PhysicalSizeYUnit": "µm",
                "Channel": {"Name": ["DAPI", "EGFP", "Cy3"]},
            },
        )
        tw.write(
            planes_512[:, :256, :256],
            photometric="minisblack",
            tile=(256, 256),
            compression="zlib",
            metadata={
                "axes": "CYX",
                "PhysicalSizeX": 0.65,
                "PhysicalSizeXUnit": "µm",
                "PhysicalSizeY": 0.65,
                "PhysicalSizeYUnit": "µm",
                "Channel": {"Name": ["DAPI", "EGFP", "Cy3"]},
            },
        )

    czi_2d = build_synthetic_czi(
        fixtures_dir / "smoke_2d.czi",
        {(0, c, 0): planes_512[c] for c in range(2)},
        compression=5,
        pixel_size_um=(0.25, 0.25),
        channels=[("DAPI", "#FF00A0FF"), ("EGFP", "#FF00FF33")],
    )

    lif_2d = build_synthetic_lif(
        fixtures_dir / "smoke_multiseries.lif",
        [
            {
                "name": "Smoke_Scene_0",
                "payload": np.ascontiguousarray(planes_512[:2]).tobytes(),
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="512" BytesInc="2" Length="{511 * 0.25e-6:.9e}" Unit="m"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="512" BytesInc="1024" Length="{511 * 0.25e-6:.9e}" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Blue" BytesInc="0"/>',
                    f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Green" BytesInc="{512 * 512 * 2}"/>',
                ],
            },
            {
                "name": "Smoke_Scene_1",
                "payload": np.ascontiguousarray(planes_512[:2, :256, :256]).tobytes(),
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="256" BytesInc="2" Length="{255 * 0.50e-6:.9e}" Unit="m"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="256" BytesInc="512" Length="{255 * 0.50e-6:.9e}" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Cyan" BytesInc="0"/>',
                    f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Magenta" BytesInc="{256 * 256 * 2}"/>',
                ],
            },
        ],
    )

    base_256 = planes_512[:2, :256, :256]
    czi_zstack = build_synthetic_czi(
        fixtures_dir / "smoke_zstack.czi",
        {
            (z, c, 0): np.roll(base_256[c], z * 2, axis=c) + np.uint16(z * 40)
            for z in range(8)
            for c in range(2)
        },
        compression=5,
        pixel_size_um=(0.25, 0.25),
        z_step_um=0.5,
        channels=[("DAPI", "#FF00A0FF"), ("EGFP", "#FF00FF33")],
    )

    return {
        "ome_2d": ome_2d,
        "czi_2d": czi_2d,
        "lif_2d": lif_2d,
        "czi_zstack": czi_zstack,
    }


def prepare_standard_fixtures(docs_dir: Path | None, fixtures_dir: Path) -> dict[str, Path]:
    """Generate/copy Milestone 1 standard benchmark fixtures inside isolated ``fixtures_dir``."""
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20261118)
    out: dict[str, Path] = {}

    if docs_dir is not None:
        cell_src = docs_dir / "example_cell.nd2"
        tissue_src = docs_dir / "example_tissue.nd2"
        if cell_src.is_file() and tissue_src.is_file():
            out["nd2_cell"] = stage_isolated_slide(cell_src, fixtures_dir)
            out["nd2_tissue"] = stage_isolated_slide(tissue_src, fixtures_dir)

    planes_2048 = np.stack(
        [_make_fluorescence_plane(rng, 2048, 2048, c) for c in range(3)], axis=0
    )

    # Multi-scene CZI 2D (Scene 0: 2048x2048 3-ch; Scene 1: 1024x1024 3-ch)
    czi_planes: dict[tuple[int, ...], np.ndarray] = {}
    for c in range(3):
        czi_planes[(0, 0, c, 0, 0, 0)] = planes_2048[c]
        czi_planes[(1, 0, c, 0, 0, 0)] = planes_2048[c, :1024, :1024]
    out["czi_2d"] = build_synthetic_czi(
        fixtures_dir / "sample_2d.czi",
        czi_planes,
        compression=5,
        pixel_size_um=(0.25, 0.25),
        channels=[("DAPI", "#FF00A0FF"), ("EGFP", "#FF00FF33"), ("Cy3", "#FFFF8000")],
    )

    # CZI Z=22 stack (1024x1024, 2-ch uint16)
    n_z = 22
    base_1024_c0 = _make_fluorescence_plane(rng, 1024, 1024, 0)
    base_1024_c1 = _make_fluorescence_plane(rng, 1024, 1024, 1)
    czi_z_planes: dict[tuple[int, ...], np.ndarray] = {}
    z_stack_zc = np.empty((n_z, 2, 1024, 1024), dtype=np.uint16)
    for z in range(n_z):
        shift = (z - n_z // 2) * 3
        p0 = np.roll(base_1024_c0, shift, axis=0) + np.uint16(z * 45)
        p1 = np.roll(base_1024_c1, -shift, axis=1) + np.uint16(z * 35)
        czi_z_planes[(z, 0, 0)] = p0
        czi_z_planes[(z, 1, 0)] = p1
        z_stack_zc[z, 0] = p0
        z_stack_zc[z, 1] = p1
    out["czi_zstack"] = build_synthetic_czi(
        fixtures_dir / "sample_zstack.czi",
        czi_z_planes,
        compression=5,
        pixel_size_um=(0.25, 0.25),
        z_step_um=0.5,
        channels=[("DAPI", "#FF00A0FF"), ("EGFP", "#FF00FF33")],
    )

    # LIF Multi-series 2D (2 scenes)
    s0_h, s0_w = 2048, 2048
    s1_h, s1_w = 1536, 1536
    s1_arr = planes_2048[:2, :s1_h, :s1_w].copy()
    out["lif_2d"] = build_synthetic_lif(
        fixtures_dir / "sample_multiseries.lif",
        [
            {
                "name": "Cortex_Overview_20x",
                "payload": np.ascontiguousarray(planes_2048).tobytes(),
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="{s0_w}" BytesInc="2" Length="{(s0_w - 1) * 0.18e-6:.9e}" Unit="m"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="{s0_h}" BytesInc="{s0_w * 2}" Length="{(s0_h - 1) * 0.18e-6:.9e}" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Blue" BytesInc="0"/>',
                    f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Green" BytesInc="{s0_h * s0_w * 2}"/>',
                    f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Red" BytesInc="{2 * s0_h * s0_w * 2}"/>',
                ],
            },
            {
                "name": "CA1_Detail_40x",
                "payload": np.ascontiguousarray(s1_arr).tobytes(),
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="{s1_w}" BytesInc="2" Length="{(s1_w - 1) * 0.11e-6:.9e}" Unit="m"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="{s1_h}" BytesInc="{s1_w * 2}" Length="{(s1_h - 1) * 0.11e-6:.9e}" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Cyan" BytesInc="0"/>',
                    f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Magenta" BytesInc="{s1_h * s1_w * 2}"/>',
                ],
            },
        ],
    )

    # LIF Z=22 stack
    zh, zw, zc = 1024, 1024, 2
    out["lif_zstack"] = build_synthetic_lif(
        fixtures_dir / "sample_zstack.lif",
        [
            {
                "name": "Confocal_ZStack_63x",
                "payload": np.ascontiguousarray(z_stack_zc).tobytes(),
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="{zw}" BytesInc="2" Length="{(zw - 1) * 0.18e-6:.9e}" Unit="m"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="{zh}" BytesInc="{zw * 2}" Length="{(zh - 1) * 0.18e-6:.9e}" Unit="m"/>',
                    f'<DimensionDescription DimID="3" NumberOfElements="{n_z}" BytesInc="{zh * zw * zc * 2}" Length="{(n_z - 1) * 0.5e-6:.9e}" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Green" BytesInc="0"/>',
                    f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Red" BytesInc="{zh * zw * 2}"/>',
                ],
            }
        ],
    )

    # Multi-scene OME-TIFF 2D
    ome_2d = fixtures_dir / "sample_2d.ome.tif"
    with tifffile.TiffWriter(str(ome_2d), bigtiff=False, ome=True) as tw:
        tw.write(
            planes_2048,
            photometric="minisblack",
            tile=(512, 512),
            compression="zlib",
            metadata={
                "axes": "CYX",
                "PhysicalSizeX": 0.325,
                "PhysicalSizeXUnit": "µm",
                "PhysicalSizeY": 0.325,
                "PhysicalSizeYUnit": "µm",
                "Channel": {"Name": ["DAPI", "EGFP", "Cy3"]},
            },
        )
        tw.write(
            planes_2048[:, :1024, :1024],
            photometric="minisblack",
            tile=(512, 512),
            compression="zlib",
            metadata={
                "axes": "CYX",
                "PhysicalSizeX": 0.65,
                "PhysicalSizeXUnit": "µm",
                "PhysicalSizeY": 0.65,
                "PhysicalSizeYUnit": "µm",
                "Channel": {"Name": ["DAPI", "EGFP", "Cy3"]},
            },
        )
    out["ome_2d"] = ome_2d

    # 72 MiB Untiled 2D OME-TIFF (4096x3072x3 uint16 = 72.0 MiB raw > 64 MiB eager ceiling)
    ome_large_untiled = fixtures_dir / "sample_large_untiled.ome.tif"
    large_cyx = np.tile(planes_2048, (1, 2, 2))[:, :3072, :4096]
    tifffile.imwrite(
        str(ome_large_untiled),
        large_cyx,
        photometric="minisblack",
        rowsperstrip=64,
        compression=None,
        ome=True,
        metadata={
            "axes": "CYX",
            "PhysicalSizeX": 0.325,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": 0.325,
            "PhysicalSizeYUnit": "µm",
            "Channel": {"Name": ["DAPI", "EGFP", "Cy3"]},
        },
    )
    del large_cyx
    out["ome_large_untiled"] = ome_large_untiled

    # OME-TIFF Z=22 stack
    ome_zstack = fixtures_dir / "sample_zstack.ome.tif"
    tifffile.imwrite(
        str(ome_zstack),
        z_stack_zc,
        photometric="minisblack",
        tile=(512, 512),
        compression="zlib",
        ome=True,
        metadata={
            "axes": "ZCYX",
            "PhysicalSizeX": 0.325,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": 0.325,
            "PhysicalSizeYUnit": "µm",
            "PhysicalSizeZ": 0.5,
            "PhysicalSizeZUnit": "µm",
            "Channel": {"Name": ["DAPI", "EGFP"]},
        },
    )
    out["ome_zstack"] = ome_zstack
    return out


def prepare_scale_fixtures(fixtures_dir: Path) -> tuple[dict[str, Path], dict[str, dict[str, Any]]]:
    """Stream-write multi-GB scale stress fixtures in isolated ``fixtures_dir``."""
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20261118)

    # Prebuild 8 structured 512x512 fluorescence tiles for streaming large TIFFs without parent RAM bloat
    tiles_512 = [_make_fluorescence_plane(rng, 512, 512, k) for k in range(8)]

    def _tile_stream(h: int, w: int, c_count: int):
        ny, nx = h // 512, w // 512
        for c in range(c_count):
            for ty in range(ny):
                for tx in range(nx):
                    yield tiles_512[(c * 3 + ty * 5 + tx) & 7]

    # 1. 16384 x 16384 x 3 uint16 = 1,536.0 MiB (1.50 GiB) raw 5-level pyramidal OME-TIFF
    ome_pyr = fixtures_dir / "scale_pyr_1536mib.ome.tif"
    with tifffile.TiffWriter(str(ome_pyr), bigtiff=True, ome=True) as tw:
        tw.write(
            _tile_stream(16384, 16384, 3),
            shape=(3, 16384, 16384),
            dtype=np.uint16,
            photometric="minisblack",
            planarconfig="separate",
            tile=(512, 512),
            compression="zlib",
            compressionargs={"level": 1},
            subifds=4,
            metadata={
                "axes": "CYX",
                "PhysicalSizeX": 0.25,
                "PhysicalSizeXUnit": "µm",
                "PhysicalSizeY": 0.25,
                "PhysicalSizeYUnit": "µm",
                "Channel": {"Name": ["DAPI", "EGFP", "Cy3"]},
            },
        )
        for lv_dim in (8192, 4096, 2048, 1024):
            tw.write(
                _tile_stream(lv_dim, lv_dim, 3),
                shape=(3, lv_dim, lv_dim),
                dtype=np.uint16,
                photometric="minisblack",
                planarconfig="separate",
                tile=(512, 512),
                compression="zlib",
                compressionargs={"level": 1},
                subfiletype=1,
            )

    # 2. 16384 x 12288 x 4 uint16 = 1,536.0 MiB (1.50 GiB) raw uncompressed untiled OME-TIFF
    # (201.3 MPx <= 256 MPx direct untiled limit; 24x above the 64 MiB eager ceiling -> _LazyUntiledLevel)
    ome_untiled = fixtures_dir / "scale_untiled_1536mib.ome.tif"
    bands_64 = [
        np.tile(tiles_512[k][:64, :], (1, 32)) + np.uint16(k * 180)
        for k in range(4)
    ]

    def _strip_stream():
        n_strips = 12288 // 64
        for c in range(4):
            for s_idx in range(n_strips):
                yield bands_64[(c + s_idx) & 3]

    with tifffile.TiffWriter(str(ome_untiled), bigtiff=True, ome=True) as tw:
        tw.write(
            _strip_stream(),
            shape=(4, 12288, 16384),
            dtype=np.uint16,
            photometric="minisblack",
            planarconfig="separate",
            rowsperstrip=64,
            compression=None,
            metadata={
                "axes": "CYX",
                "PhysicalSizeX": 0.325,
                "PhysicalSizeXUnit": "µm",
                "PhysicalSizeY": 0.325,
                "PhysicalSizeYUnit": "µm",
                "Channel": {"Name": ["DAPI", "EGFP", "Cy3", "Cy5"]},
            },
        )
    bands_64.clear()

    # 3. 12288 x 12288 x 3 uint16 = 864.0 MiB raw 6x6 mosaic Zstd CZI (108 SubBlocks of 2048x2048)
    mosaic_tiles_zstd = [
        imagecodecs.zstd_encode(
            np.ascontiguousarray(_make_fluorescence_plane(rng, 2048, 2048, k)).tobytes()
        )
        for k in range(6)
    ]
    mosaic_planes: dict[tuple[int, ...], bytes] = {}
    for c in range(3):
        for gy in range(6):
            for gx in range(6):
                m_idx = gy * 6 + gx
                mosaic_planes[(0, c, m_idx, gx * 2048, gy * 2048)] = mosaic_tiles_zstd[
                    (c * 2 + gy + gx) % 6
                ]
    czi_mosaic = build_synthetic_czi(
        fixtures_dir / "scale_mosaic_864mib.czi",
        mosaic_planes,
        tile_shape=(2048, 2048),
        compression=5,
        pixel_size_um=(0.25, 0.25),
        channels=[("DAPI", "#FF00A0FF"), ("EGFP", "#FF00FF33"), ("Cy3", "#FFFF8000")],
    )

    # 4. 2048 x 2048 x 3 uint16 Z=32 = 768.0 MiB raw Zstd CZI confocal Z-stack (96 SubBlocks)
    z32_planes: dict[tuple[int, ...], bytes] = {}
    for z in range(32):
        for c in range(3):
            z32_planes[(z, c, 0)] = mosaic_tiles_zstd[(z + c * 2) % 6]
    czi_z32 = build_synthetic_czi(
        fixtures_dir / "scale_zstack_z32.czi",
        z32_planes,
        tile_shape=(2048, 2048),
        compression=5,
        pixel_size_um=(0.25, 0.25),
        z_step_um=0.45,
        channels=[("DAPI", "#FF00A0FF"), ("EGFP", "#FF00FF33"), ("Cy3", "#FFFF8000")],
    )
    mosaic_tiles_zstd.clear()

    paths = {
        "ome_pyr_1536mib": ome_pyr,
        "ome_untiled_1536mib": ome_untiled,
        "czi_mosaic_864mib": czi_mosaic,
        "czi_zstack_z32": czi_z32,
    }
    specs = {
        "ome_pyr_1536mib": {
            "dimensions": "16384x16384",
            "channels": 3,
            "z_planes": 1,
            "dtype": "uint16",
            "raw_level0_mib": 1536.0,
            "disk_mib": round(ome_pyr.stat().st_size / (1024.0 * 1024.0), 2),
        },
        "ome_untiled_1536mib": {
            "dimensions": "16384x12288",
            "channels": 4,
            "z_planes": 1,
            "dtype": "uint16",
            "raw_level0_mib": 1536.0,
            "disk_mib": round(ome_untiled.stat().st_size / (1024.0 * 1024.0), 2),
        },
        "czi_mosaic_864mib": {
            "dimensions": "12288x12288",
            "channels": 3,
            "mosaic_tiles": 36,
            "z_planes": 1,
            "dtype": "uint16",
            "raw_level0_mib": 864.0,
            "disk_mib": round(czi_mosaic.stat().st_size / (1024.0 * 1024.0), 2),
        },
        "czi_zstack_z32": {
            "dimensions": "2048x2048",
            "channels": 3,
            "z_planes": 32,
            "dtype": "uint16",
            "raw_stack_mib": 768.0,
            "disk_mib": round(czi_z32.stat().st_size / (1024.0 * 1024.0), 2),
        },
    }
    return paths, specs


# ---------------------------------------------------------------------------
# HTTP Client Utilities (in-child-process)
# ---------------------------------------------------------------------------


def _http_request(
    base_url: str,
    method: str,
    rel_path: str,
    body_obj: dict[str, Any] | None = None,
    *,
    timeout: float = 180.0,
) -> tuple[int, bytes, dict[str, str]]:
    parsed = urllib.parse.urlparse(base_url.rstrip("/"))
    full_path = parsed.path + rel_path
    conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port, timeout=timeout)
    try:
        headers = {"Connection": "close"}
        payload = None
        if body_obj is not None:
            payload = json.dumps(body_obj).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        conn.request(method, full_path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, data, resp_headers
    finally:
        conn.close()


class ConcurrentTileClient:
    """Persistent HTTP/1.1 keep-alive pool for 16-tile concurrent viewport bursts."""

    def __init__(self, base_url: str, workers: int = 16) -> None:
        parsed = urllib.parse.urlparse(base_url.rstrip("/"))
        self.host = parsed.hostname or "127.0.0.1"
        self.port = int(parsed.port)
        self.prefix = parsed.path
        self.workers = workers
        self._tls = threading.local()
        self._conns_lock = threading.Lock()
        self._conns: list[http.client.HTTPConnection] = []
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bench-http")

    def _get_conn(self) -> http.client.HTTPConnection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=60.0)
            self._tls.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    def _fetch_one(self, rel_path: str) -> int:
        for attempt in range(2):
            conn = self._get_conn()
            try:
                conn.request("GET", self.prefix + rel_path, headers={"Connection": "keep-alive"})
                resp = conn.getresponse()
                body = resp.read()
                if resp.status != 200 or len(body) < 32:
                    raise RuntimeError(
                        f"tile {rel_path} returned HTTP {resp.status} ({len(body)}B)"
                    )
                return len(body)
            except (http.client.HTTPException, OSError):
                if attempt == 1:
                    raise
                try:
                    conn.close()
                except Exception:
                    pass
                self._tls.conn = None
        return 0

    def fetch_burst(self, rel_paths: list[str]) -> int:
        futs = [self._pool.submit(self._fetch_one, p) for p in rel_paths]
        return sum(f.result() for f in futs)

    def close(self) -> None:
        with self._conns_lock:
            conns = list(self._conns)
            self._conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        self._pool.shutdown(wait=True)

    def __enter__(self) -> ConcurrentTileClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _start_server() -> tuple[Any, threading.Thread, str]:
    from nd2wsi.server import create_server, server_url

    srv = create_server([], port=0)
    t = threading.Thread(
        target=srv.serve_forever,
        kwargs={"poll_interval": 0.02},
        name="bench-srv",
        daemon=True,
    )
    t.start()
    return srv, t, server_url(srv).rstrip("/")


def _stop_server(srv: Any, thread: threading.Thread) -> None:
    try:
        srv.shutdown()
    finally:
        srv.server_close()
        thread.join(timeout=5.0)


def _format_win(
    channels: list[dict[str, Any]],
    delta_start: float = 0.0,
    scale_end: float = 1.0,
    gamma: float = 1.0,
) -> str:
    parts = []
    for idx, ch in enumerate(channels):
        w = ch["window"]
        lo = max(0.0, float(w["start"]) + delta_start)
        hi = max(lo + 1.0, float(w["end"]) * scale_end)
        parts.append(f"{idx}:{lo:.1f}:{hi:.1f}:{gamma:.2f}")
    return ",".join(parts)


# ---------------------------------------------------------------------------
# Child-Process Scenario Runners
# ---------------------------------------------------------------------------


def child_open_and_tiles(spec: dict[str, Any]) -> dict[str, Any]:
    """Run Cold Open, Warm Open, and 16-tile Viewport Burst + Live LUT Update."""
    from nd2wsi import render

    fixture_path = Path(spec["fixture_path"])
    work_dir = Path(spec["work_dir"])
    n_cold = int(spec.get("n_cold", 5))
    n_warm = int(spec.get("n_warm", 12))
    n_burst = int(spec.get("n_burst", 20))
    tile_level = int(spec.get("tile_level", 0))
    export_warm_dir = Path(spec["export_warm_dir"]) if spec.get("export_warm_dir") else None

    with RssSampler() as rss:
        cold_ms: list[float] = []
        warm_ms: list[float] = []
        burst_init_ms: list[float] = []
        burst_lut_ms: list[float] = []

        kept_dir: Path | None = None
        kept_slide: Path | None = None
        try:
            for i in range(n_cold):
                iter_dir = work_dir / f"cold_{i}"
                slide_copy = stage_isolated_slide(fixture_path, iter_dir)
                srv, th, base = _start_server()
                try:
                    t0 = time.perf_counter()
                    code, raw, _ = _http_request(
                        base, "POST", "/api/open", {"path": str(slide_copy)}
                    )
                    if code != 200:
                        err_txt = raw.decode("utf-8", errors="replace")
                        shutil.rmtree(iter_dir, ignore_errors=True)
                        return {
                            "supported": False,
                            "http_status": code,
                            "error": err_txt,
                            "rss": rss.report(),
                        }
                    sid = json.loads(raw)["sid"]
                    code_i, info_raw, _ = _http_request(base, "GET", f"/s/{sid}/api/info")
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    if code_i != 200:
                        raise RuntimeError(f"GET /api/info failed ({code_i})")
                    cold_ms.append(elapsed_ms)
                finally:
                    _stop_server(srv, th)
                if i == n_cold - 1:
                    kept_dir = iter_dir
                    kept_slide = slide_copy
                else:
                    shutil.rmtree(iter_dir, ignore_errors=True)

            assert kept_slide is not None and kept_dir is not None

            info_payload: dict[str, Any] = {}
            for _ in range(n_warm):
                if hasattr(render, "clear_raw_chunk_cache"):
                    render.clear_raw_chunk_cache()
                srv, th, base = _start_server()
                try:
                    t0 = time.perf_counter()
                    code, raw, _ = _http_request(
                        base, "POST", "/api/open", {"path": str(kept_slide)}
                    )
                    if code != 200:
                        raise RuntimeError(f"Warm open failed ({code}): {raw!r}")
                    sid = json.loads(raw)["sid"]
                    code_i, info_raw, _ = _http_request(base, "GET", f"/s/{sid}/api/info")
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    if code_i != 200:
                        raise RuntimeError(f"Warm info failed ({code_i})")
                    warm_ms.append(elapsed_ms)
                    info_payload = json.loads(info_raw)
                finally:
                    _stop_server(srv, th)

            inspect_payload: dict[str, Any] = {}
            if n_burst > 0:
                srv, th, base = _start_server()
                try:
                    code, raw, _ = _http_request(
                        base, "POST", "/api/open", {"path": str(kept_slide)}
                    )
                    sid = json.loads(raw)["sid"]
                    _, info_raw, _ = _http_request(base, "GET", f"/s/{sid}/api/info")
                    info_payload = json.loads(info_raw)
                    ci_code, insp_raw, _ = _http_request(base, "GET", f"/s/{sid}/api/inspect")
                    if ci_code == 200:
                        inspect_payload = json.loads(insp_raw)
                    gen = info_payload["generation"]
                    channels = info_payload["channels"]
                    levels = info_payload["levels"]
                    lv_entry = next(
                        (lv for lv in levels if str(lv["path"]) == str(tile_level)), levels[0]
                    )
                    actual_level = int(lv_entry["path"])
                    tile_sz = int(info_payload["tileSize"])
                    nx = max(1, -(-int(lv_entry["width"]) // tile_sz))
                    ny = max(1, -(-int(lv_entry["height"]) // tile_sz))
                    coords = [(tx % nx, ty % ny) for ty in range(4) for tx in range(4)]

                    is_plate = info_payload.get("kind") == "plate"
                    z_home = int((info_payload.get("plate") or {}).get("zHome", 0))
                    frame_q = f"&t=0&p=0&z={z_home}" if is_plate else ""

                    with ConcurrentTileClient(base, workers=16) as client:
                        win0 = _format_win(channels, delta_start=0.0, scale_end=1.0, gamma=1.0)
                        client.fetch_burst(
                            [
                                f"/s/{sid}/api/tile/{actual_level}/{tx}/{ty}.jpg?g={gen}&win={urllib.parse.quote(win0)}{frame_q}"
                                for tx, ty in coords
                            ]
                        )

                        for b_idx in range(n_burst):
                            if hasattr(render, "clear_raw_chunk_cache"):
                                render.clear_raw_chunk_cache()
                            win_init = _format_win(
                                channels, delta_start=float(b_idx * 3), scale_end=1.0, gamma=1.0
                            )
                            win_drag = _format_win(
                                channels,
                                delta_start=float(b_idx * 3 + 65),
                                scale_end=0.82,
                                gamma=1.08,
                            )
                            init_paths = [
                                f"/s/{sid}/api/tile/{actual_level}/{tx}/{ty}.jpg?g={gen}&win={urllib.parse.quote(win_init)}{frame_q}"
                                for tx, ty in coords
                            ]
                            drag_paths = [
                                f"/s/{sid}/api/tile/{actual_level}/{tx}/{ty}.jpg?g={gen}&win={urllib.parse.quote(win_drag)}{frame_q}"
                                for tx, ty in coords
                            ]
                            t0 = time.perf_counter()
                            client.fetch_burst(init_paths)
                            burst_init_ms.append((time.perf_counter() - t0) * 1000.0)

                            t1 = time.perf_counter()
                            client.fetch_burst(drag_paths)
                            burst_lut_ms.append((time.perf_counter() - t1) * 1000.0)

                    cache_stats = (
                        render.raw_chunk_cache_stats()
                        if hasattr(render, "raw_chunk_cache_stats")
                        else None
                    )

                    scene_switch_cold_ms: float | None = None
                    scene_switch_warm_ms: list[float] = []
                    if int(info_payload.get("seriesCount") or 1) > 1:
                        n_sc_switches = int(spec.get("n_scene_switches", 6))
                        cur_sid = sid
                        t_sc0 = time.perf_counter()
                        sc_c, sc_r, _ = _http_request(
                            base, "POST", f"/s/{cur_sid}/api/select-scene", {"series": 1}
                        )
                        if sc_c == 200:
                            cur_sid = json.loads(sc_r)["sid"]
                            _http_request(base, "GET", f"/s/{cur_sid}/api/info")
                            scene_switch_cold_ms = round((time.perf_counter() - t_sc0) * 1000.0, 2)
                            for sw_i in range(n_sc_switches):
                                target_s = 0 if (sw_i % 2 == 0) else 1
                                t_sw = time.perf_counter()
                                sw_c, sw_r, _ = _http_request(
                                    base,
                                    "POST",
                                    f"/s/{cur_sid}/api/select-scene",
                                    {"series": target_s},
                                )
                                if sw_c != 200:
                                    break
                                cur_sid = json.loads(sw_r)["sid"]
                                _http_request(base, "GET", f"/s/{cur_sid}/api/info")
                                scene_switch_warm_ms.append((time.perf_counter() - t_sw) * 1000.0)
                finally:
                    _stop_server(srv, th)
            else:
                cache_stats = None
                scene_switch_cold_ms = None
                scene_switch_warm_ms = []

            if export_warm_dir is not None and kept_dir is not None:
                if export_warm_dir.exists():
                    shutil.rmtree(export_warm_dir, ignore_errors=True)
                shutil.move(str(kept_dir), str(export_warm_dir))
                kept_dir = None
        finally:
            if kept_dir is not None:
                shutil.rmtree(kept_dir, ignore_errors=True)
            shutil.rmtree(work_dir, ignore_errors=True)

    plate_info = info_payload.get("plate") or {}
    cache_bytes = inspect_payload.get("cache_bytes")
    return {
        "supported": True,
        "storage": info_payload.get("storage"),
        "kind": info_payload.get("kind"),
        "width": info_payload.get("width"),
        "height": info_payload.get("height"),
        "dtype": info_payload.get("dtype"),
        "channels_count": len(info_payload.get("channels") or []),
        "z_planes": int(plate_info.get("Z") or plate_info.get("zCount") or 1) if plate_info else 1,
        "pixel_size_um": info_payload.get("pixelSizeUm"),
        "objective_magnification": inspect_payload.get("objective_magnification"),
        "cache_mib": round(float(cache_bytes) / (1024.0 * 1024.0), 2) if cache_bytes else None,
        "series_count": info_payload.get("seriesCount", 1),
        "notes": info_payload.get("notes", []),
        "cold_open": summarize_ms(cold_ms),
        "warm_open": summarize_ms(warm_ms),
        "tile_burst_initial_16": summarize_ms(burst_init_ms),
        "tile_burst_lut_update_16": summarize_ms(burst_lut_ms),
        "scene_switch_cold_ms": scene_switch_cold_ms,
        "scene_switch_warm": summarize_ms(scene_switch_warm_ms),
        "raw_chunk_cache_stats": cache_stats,
        "rss": rss.report(),
    }


def child_zstack_bench(spec: dict[str, Any]) -> dict[str, Any]:
    """Measure Z-stack cold plane jump, prefetched z+1 step, and rapid Z-scrub."""
    from nd2wsi import plate as plate_mod

    fixture_path = Path(spec["fixture_path"])
    work_dir = Path(spec["work_dir"])
    n_jumps = int(spec.get("n_jumps", 15))
    n_steps = int(spec.get("n_steps", 15))
    n_scrubs = int(spec.get("n_scrubs", 7))
    slide_copy = stage_isolated_slide(fixture_path, work_dir)

    with RssSampler() as rss:
        srv, th, base = _start_server()
        try:
            code, raw, _ = _http_request(base, "POST", "/api/open", {"path": str(slide_copy)})
            if code != 200:
                return {
                    "supported": False,
                    "http_status": code,
                    "error": raw.decode("utf-8", errors="replace"),
                    "rss": rss.report(),
                }
            sid = json.loads(raw)["sid"]
            _, info_raw, _ = _http_request(base, "GET", f"/s/{sid}/api/info")
            info = json.loads(info_raw)
            gen = info["generation"]
            win_str = urllib.parse.quote(_format_win(info["channels"]))
            st = srv.registry.slides[sid]
            psrc = st.plate
            if psrc is None or int(getattr(psrc, "Z", 1)) <= 1:
                return {
                    "supported": False,
                    "error": "Slide did not open as a multi-Z interactive stack",
                    "rss": rss.report(),
                }
            n_z = int(psrc.Z)
            tile_sz = int(info["tileSize"])
            nx = max(1, -(-int(info["width"]) // tile_sz))
            ny = max(1, -(-int(info["height"]) // tile_sz))
            coords = [(tx, ty) for ty in range(min(2, ny)) for tx in range(min(2, nx))]

            def _clear_frame_caches() -> None:
                plate_mod._FRAME_CACHE.purge(psrc._owner)
                try:
                    from nd2wsi import ometiff as ometiff_mod

                    ometiff_mod._STACK_FRAME_CACHE.purge(psrc._owner)
                except Exception:
                    pass
                try:
                    from nd2wsi import render as render_mod

                    if hasattr(render_mod, "clear_raw_chunk_cache"):
                        render_mod.clear_raw_chunk_cache()
                except Exception:
                    pass

            cold_z_ms: list[float] = []
            step_z_ms: list[float] = []
            scrub_final_ms: list[float] = []
            scrub_decoded_counts: list[int] = []
            scrub_steps = max(1, min(n_z - 2 if n_z >= 3 else 1, int(spec.get("scrub_steps", n_z - 2))))

            with ConcurrentTileClient(base, workers=max(1, len(coords))) as client:
                client.fetch_burst(
                    [
                        f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={win_str}&t=0&p=0&z=0"
                        for tx, ty in coords
                    ]
                )

                for idx in range(n_jumps):
                    _clear_frame_caches()
                    target_z = (idx * 3 + 1) % n_z
                    paths = [
                        f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={win_str}&t=0&p=0&z={target_z}"
                        for tx, ty in coords
                    ]
                    t0 = time.perf_counter()
                    client.fetch_burst(paths)
                    cold_z_ms.append((time.perf_counter() - t0) * 1000.0)

                max_base = max(1, n_z - 2)
                for idx in range(n_steps):
                    _clear_frame_caches()
                    base_z = idx % max_base
                    next_z = min(n_z - 1, base_z + 1)
                    base_paths = [
                        f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={win_str}&t=0&p=0&z={base_z}"
                        for tx, ty in coords
                    ]
                    next_paths = [
                        f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={win_str}&t=0&p=0&z={next_z}"
                        for tx, ty in coords
                    ]
                    client.fetch_burst(base_paths)
                    time.sleep(0.080)
                    t0 = time.perf_counter()
                    client.fetch_burst(next_paths)
                    step_z_ms.append((time.perf_counter() - t0) * 1000.0)

                decode_lock = threading.Lock()
                decode_counter = [0]
                backend_kind = getattr(psrc, "_backend", None)
                if hasattr(psrc, "_read_plane_cyx"):
                    orig_fn_name = "_read_plane_cyx"
                elif backend_kind == "czi":
                    orig_fn_name = "_read_czi_frame"
                elif backend_kind == "lif":
                    orig_fn_name = "_read_lif_frame"
                else:
                    orig_fn_name = "_read_raw"
                orig_fn = getattr(psrc, orig_fn_name)

                def _counting_read(*args: Any, **kwargs: Any) -> Any:
                    with decode_lock:
                        decode_counter[0] += 1
                    return orig_fn(*args, **kwargs)

                setattr(psrc, orig_fn_name, _counting_read)
                try:
                    for _ in range(n_scrubs):
                        _clear_frame_caches()
                        time.sleep(0.040)
                        with decode_lock:
                            decode_counter[0] = 0
                        for z_scrub in range(scrub_steps):
                            if hasattr(psrc, "prefetch_neighbors"):
                                psrc.prefetch_neighbors(0, 0, z_scrub)
                            time.sleep(0.003)
                        final_paths = [
                            f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={win_str}&t=0&p=0&z={scrub_steps}"
                            for tx, ty in coords
                        ]
                        t0 = time.perf_counter()
                        client.fetch_burst(final_paths)
                        scrub_final_ms.append((time.perf_counter() - t0) * 1000.0)
                        time.sleep(0.120)
                        with decode_lock:
                            scrub_decoded_counts.append(int(decode_counter[0]))
                finally:
                    setattr(psrc, orig_fn_name, orig_fn)
        finally:
            _stop_server(srv, th)
            lingering_zpf = [
                t.name for t in threading.enumerate() if "z-prefetch" in t.name and t.is_alive()
            ]
            shutil.rmtree(work_dir, ignore_errors=True)

    decoded_summary = {
        "scrub_steps": scrub_steps,
        "median": float(statistics.median(scrub_decoded_counts)),
        "max": int(max(scrub_decoded_counts)),
        "samples": scrub_decoded_counts,
    }
    return {
        "supported": True,
        "z_planes": n_z,
        "scrub_steps": scrub_steps,
        "cold_z_jump_4tiles": summarize_ms(cold_z_ms),
        "step_z_plus_1_4tiles": summarize_ms(step_z_ms),
        "rapid_scrub_final_4tiles": summarize_ms(scrub_final_ms),
        "rapid_scrub_decoded_planes": decoded_summary,
        "lingering_prefetch_threads": len(lingering_zpf),
        "rss": rss.report(),
    }


def child_milestone_roundtrip(spec: dict[str, Any]) -> dict[str, Any]:
    """End-to-end Milestone 1 round-trip:
    open -> scene select (if multi-scene) -> export OME-TIFF ROI -> reopen .ome.tif -> save & reload sidecar.
    """
    fixture_path = Path(spec["fixture_path"])
    work_dir = Path(spec["work_dir"])
    n_iters = int(spec.get("n_iters", 7))
    prewarmed_dir = Path(spec["prewarmed_dir"]) if spec.get("prewarmed_dir") else None

    total_ms: list[float] = []
    open_scene_ms: list[float] = []
    export_ms: list[float] = []
    reopen_sidecar_ms: list[float] = []
    reopen_supported = True
    reopen_error = ""

    with RssSampler() as rss:
        try:
            for i in range(n_iters):
                iter_dir = work_dir / f"rt_{i}"
                if prewarmed_dir is not None and prewarmed_dir.is_dir():
                    shutil.copytree(prewarmed_dir, iter_dir, copy_function=_clone_or_copy_file)
                    slide_copy = iter_dir / fixture_path.name
                else:
                    slide_copy = stage_isolated_slide(fixture_path, iter_dir)
                srv, th, base = _start_server()
                try:
                    t_start = time.perf_counter()
                    code, raw, _ = _http_request(
                        base, "POST", "/api/open", {"path": str(slide_copy)}
                    )
                    if code != 200:
                        return {
                            "supported": False,
                            "stage_failed": "open_source",
                            "http_status": code,
                            "error": raw.decode("utf-8", errors="replace"),
                            "rss": rss.report(),
                        }
                    sid = json.loads(raw)["sid"]
                    _, info_raw, _ = _http_request(base, "GET", f"/s/{sid}/api/info")
                    info = json.loads(info_raw)

                    if int(info.get("seriesCount") or 1) > 1:
                        sc_code, sc_raw, _ = _http_request(
                            base, "POST", f"/s/{sid}/api/select-scene", {"series": 1}
                        )
                        if sc_code == 200:
                            sid = json.loads(sc_raw)["sid"]
                            _, info_raw, _ = _http_request(base, "GET", f"/s/{sid}/api/info")
                            info = json.loads(info_raw)
                    t_after_scene = time.perf_counter()
                    open_scene_ms.append((t_after_scene - t_start) * 1000.0)

                    img_w = int(info["width"])
                    img_h = int(info["height"])
                    roi_w = min(512, img_w)
                    roi_h = min(512, img_h)
                    roi_x = min(128, max(0, img_w - roi_w))
                    roi_y = min(128, max(0, img_h - roi_h))

                    is_plate = info.get("kind") == "plate"
                    z_home = int((info.get("plate") or {}).get("zHome", 0))
                    fq = f"&t=0&p=0&z={z_home}" if is_plate else ""
                    code_exp, tif_bytes, _ = _http_request(
                        base,
                        "GET",
                        f"/s/{sid}/api/roi?level=0&x={roi_x}&y={roi_y}&w={roi_w}&h={roi_h}&format=tif{fq}",
                    )
                    if code_exp != 200 or len(tif_bytes) < 128:
                        raise RuntimeError(
                            f"ROI TIFF export failed ({code_exp}, {len(tif_bytes)}B)"
                        )
                    roi_path = iter_dir / f"{slide_copy.stem}_roi.ome.tif"
                    roi_path.write_bytes(tif_bytes)
                    t_after_export = time.perf_counter()
                    export_ms.append((t_after_export - t_after_scene) * 1000.0)

                    code_re, re_raw, _ = _http_request(
                        base, "POST", "/api/open", {"path": str(roi_path)}
                    )
                    if code_re != 200:
                        reopen_supported = False
                        reopen_error = re_raw.decode("utf-8", errors="replace")
                        continue

                    roi_sid = json.loads(re_raw)["sid"]
                    _, roi_info_raw, _ = _http_request(base, "GET", f"/s/{roi_sid}/api/info")
                    roi_info = json.loads(roi_info_raw)
                    assert roi_info["width"] == roi_w and roi_info["height"] == roi_h

                    code_tile, _, _ = _http_request(base, "GET", f"/s/{roi_sid}/api/tile/0/0/0.jpg")
                    if code_tile != 200:
                        raise RuntimeError(f"Reopened ROI tile failed ({code_tile})")

                    ann_items = [
                        {
                            "id": "bench-roi-1",
                            "type": "rect",
                            "x": min(16, max(0, roi_w - 8)),
                            "y": min(16, max(0, roi_h - 8)),
                            "w": max(4, min(120, roi_w // 2)),
                            "h": max(4, min(96, roi_h // 2)),
                            "label": "Validated_ROI",
                            "color": "#00ff88",
                        }
                    ]
                    code_save, save_raw, _ = _http_request(
                        base, "POST", f"/s/{roi_sid}/api/annotations", {"items": ann_items}
                    )
                    if code_save != 200:
                        raise RuntimeError(
                            f"Annotation save on reopened ROI failed ({code_save}): {save_raw!r}"
                        )
                    assert json.loads(save_raw).get("ok") is True

                    _http_request(base, "POST", "/api/close", {"sid": roi_sid})
                    code_re2, re_raw2, _ = _http_request(
                        base, "POST", "/api/open", {"path": str(roi_path)}
                    )
                    roi_sid2 = json.loads(re_raw2)["sid"]
                    code_get, get_raw, _ = _http_request(
                        base, "GET", f"/s/{roi_sid2}/api/annotations"
                    )
                    loaded = json.loads(get_raw)
                    if code_get != 200 or len(loaded.get("items") or []) != 1:
                        raise RuntimeError(f"Annotation reload failed ({code_get}): {loaded}")

                    t_end = time.perf_counter()
                    reopen_sidecar_ms.append((t_end - t_after_export) * 1000.0)
                    total_ms.append((t_end - t_start) * 1000.0)
                finally:
                    _stop_server(srv, th)
                    shutil.rmtree(iter_dir, ignore_errors=True)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    return {
        "supported": True,
        "reopen_roi_supported": reopen_supported,
        "reopen_error": reopen_error,
        "open_and_scene_ms": summarize_ms(open_scene_ms),
        "export_roi_tiff_ms": summarize_ms(export_ms),
        "reopen_and_sidecar_ms": summarize_ms(reopen_sidecar_ms),
        "end_to_end_total_ms": summarize_ms(total_ms),
        "rss": rss.report(),
    }


def child_memory_stress(spec: dict[str, Any]) -> dict[str, Any]:
    """Measure clean Peak RSS in a dedicated fresh subprocess for:
    - ``single``: single slide open + 16-tile burst + LUT update
    - ``four_concurrent``: 4 slides open simultaneously + concurrent viewport & LUT bursts + Z-scrub
    - ``large_untiled_ometiff``: large untiled 2D OME-TIFF open + 16-tile Level 0, LUT drag & overview burst
    """
    mode = spec["mode"]
    work_dir = Path(spec["work_dir"])
    work_dir.mkdir(parents=True, exist_ok=True)
    fixture_entries = spec["fixture_entries"]

    with RssSampler(interval_s=0.015) as rss:
        copies: list[Path] = []
        try:
            for idx, entry in enumerate(fixture_entries):
                sub = work_dir / f"s_{idx}"
                if isinstance(entry, dict) and entry.get("prewarmed_dir"):
                    pw = Path(entry["prewarmed_dir"])
                    shutil.copytree(pw, sub, copy_function=_clone_or_copy_file)
                    copies.append(sub / Path(entry["path"]).name)
                else:
                    fp = Path(entry["path"] if isinstance(entry, dict) else entry)
                    copies.append(stage_isolated_slide(fp, sub))

            srv, th, base = _start_server()
            try:
                sids_infos: list[tuple[str, dict[str, Any]]] = []
                for dst in copies:
                    code, raw, _ = _http_request(base, "POST", "/api/open", {"path": str(dst)})
                    if code != 200:
                        return {
                            "supported": False,
                            "http_status": code,
                            "error": raw.decode("utf-8", errors="replace"),
                            "rss": rss.report(),
                        }
                    sid = json.loads(raw)["sid"]
                    _, info_raw, _ = _http_request(base, "GET", f"/s/{sid}/api/info")
                    sids_infos.append((sid, json.loads(info_raw)))

                if mode == "single":
                    sid, info = sids_infos[0]
                    gen = info["generation"]
                    is_plate = info.get("kind") == "plate"
                    z_home = int((info.get("plate") or {}).get("zHome", 0))
                    fq = f"&t=0&p=0&z={z_home}" if is_plate else ""
                    nx = max(1, -(-int(info["width"]) // int(info["tileSize"])))
                    ny = max(1, -(-int(info["height"]) // int(info["tileSize"])))
                    coords = [(tx % nx, ty % ny) for ty in range(4) for tx in range(4)]
                    w1 = urllib.parse.quote(_format_win(info["channels"]))
                    w2 = urllib.parse.quote(
                        _format_win(info["channels"], delta_start=80, scale_end=0.85)
                    )
                    with ConcurrentTileClient(base, workers=16) as client:
                        client.fetch_burst(
                            [
                                f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={w1}{fq}"
                                for tx, ty in coords
                            ]
                        )
                        client.fetch_burst(
                            [
                                f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={w2}{fq}"
                                for tx, ty in coords
                            ]
                        )

                elif mode == "four_concurrent":
                    all_paths: list[str] = []
                    all_drag_paths: list[str] = []
                    plate_sids: list[tuple[str, dict[str, Any]]] = []
                    for sid, info in sids_infos:
                        gen = info["generation"]
                        is_plate = info.get("kind") == "plate"
                        w1 = urllib.parse.quote(_format_win(info["channels"]))
                        w2 = urllib.parse.quote(
                            _format_win(info["channels"], delta_start=75, scale_end=0.84)
                        )
                        nx = max(1, -(-int(info["width"]) // int(info["tileSize"])))
                        ny = max(1, -(-int(info["height"]) // int(info["tileSize"])))
                        coords = [(tx % nx, ty % ny) for ty in range(3) for tx in range(3)][:8]
                        if is_plate:
                            plate_sids.append((sid, info))
                            z_cnt = max(1, int((info.get("plate") or {}).get("zCount", 1)))
                            for z_idx in range(min(3, z_cnt)):
                                for tx, ty in coords[:4]:
                                    all_paths.append(
                                        f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={w1}&t=0&p=0&z={z_idx}"
                                    )
                                    all_drag_paths.append(
                                        f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={w2}&t=0&p=0&z={z_idx}"
                                    )
                        else:
                            for tx, ty in coords:
                                all_paths.append(
                                    f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={w1}"
                                )
                                all_drag_paths.append(
                                    f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={w2}"
                                )

                    with ConcurrentTileClient(base, workers=16) as client:
                        for _ in range(3):
                            client.fetch_burst(all_paths)
                            client.fetch_burst(all_drag_paths)
                        for sid, info in plate_sids:
                            psrc = srv.registry.slides[sid].plate
                            z_cnt = max(1, int(getattr(psrc, "Z", 1)))
                            if z_cnt > 4 and hasattr(psrc, "prefetch_neighbors"):
                                for z_s in range(min(20, z_cnt - 1)):
                                    psrc.prefetch_neighbors(0, 0, z_s)
                                    time.sleep(0.002)
                                gen = info["generation"]
                                w1 = urllib.parse.quote(_format_win(info["channels"]))
                                target_z = min(20, z_cnt - 1)
                                client.fetch_burst(
                                    [
                                        f"/s/{sid}/api/tile/0/0/0.jpg?g={gen}&win={w1}&t=0&p=0&z={target_z}"
                                    ]
                                )

                elif mode == "large_untiled_ometiff":
                    sid, info = sids_infos[0]
                    gen = info["generation"]
                    w1 = urllib.parse.quote(_format_win(info["channels"]))
                    w2 = urllib.parse.quote(
                        _format_win(info["channels"], delta_start=60, scale_end=0.86)
                    )
                    top_lv = int(info["levels"][-1]["path"])
                    nx = max(1, -(-int(info["width"]) // int(info["tileSize"])))
                    ny = max(1, -(-int(info["height"]) // int(info["tileSize"])))
                    coords = [(tx % nx, ty % ny) for ty in range(4) for tx in range(4)]
                    with ConcurrentTileClient(base, workers=16) as client:
                        client.fetch_burst(
                            [f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={w1}" for tx, ty in coords]
                        )
                        client.fetch_burst(
                            [f"/s/{sid}/api/tile/0/{tx}/{ty}.jpg?g={gen}&win={w2}" for tx, ty in coords]
                        )
                        client.fetch_burst([f"/s/{sid}/api/tile/{top_lv}/0/0.jpg?g={gen}&win={w1}"])
            finally:
                _stop_server(srv, th)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    return {"supported": True, "mode": mode, "rss": rss.report()}


# ---------------------------------------------------------------------------
# Subprocess Isolation Orchestrator
# ---------------------------------------------------------------------------


def run_child_scenario(
    repo_root: Path,
    bench_tmp: Path,
    scenario: str,
    spec: dict[str, Any],
    *,
    timeout: int = 600,
) -> dict[str, Any]:
    """Spawn a clean, isolated Python subprocess pinned to ``repo_root``."""
    child_iso = bench_tmp / f"iso_{scenario}_{time.time_ns()}_{threading.get_ident()}"
    tmp_dir = child_iso / "tmp"
    app_sup = child_iso / "app_support"
    pyc_dir = bench_tmp / "shared_pycache"
    out_json = child_iso / "result.json"
    for d in (tmp_dir, app_sup, pyc_dir):
        d.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    env["TMPDIR"] = str(tmp_dir)
    env["TEMP"] = str(tmp_dir)
    env["TMP"] = str(tmp_dir)
    env["ND2WSI_APP_SUPPORT_DIR"] = str(app_sup)
    env["ND2WSI_UI_SETTINGS_ROOT"] = str(app_sup)
    env["ND2WSI_LUT_SETTINGS_ROOT"] = str(app_sup / "lut-settings")
    env["ND2WSI_WINDOW_SESSION_ROOT"] = str(app_sup / "window-sessions")
    env["PYTHONPYCACHEPREFIX"] = str(pyc_dir)
    env.pop("PYTHONDONTWRITEBYTECODE", None)

    spec_path = child_iso / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--target-root",
        str(repo_root.resolve()),
        "--child-scenario",
        scenario,
        "--child-spec",
        str(spec_path),
        "--child-out",
        str(out_json),
    ]
    try:
        proc = subprocess.run(
            cmd, env=env, cwd=str(tmp_dir), capture_output=True, text=True, timeout=timeout
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Child scenario {scenario} failed ({proc.returncode}):\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        return json.loads(out_json.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(child_iso, ignore_errors=True)


# ---------------------------------------------------------------------------
# Suite Runners
# ---------------------------------------------------------------------------


def _evaluate_gate(four_tab_peaks_mib: list[float], max_4tab_rss_mib: float) -> dict[str, Any]:
    observed_max = round(float(max(four_tab_peaks_mib)), 1) if four_tab_peaks_mib else 0.0
    passed = bool(four_tab_peaks_mib) and observed_max <= float(max_4tab_rss_mib)
    return {
        "max_4tab_rss_mib_limit": round(float(max_4tab_rss_mib), 1),
        "observed_4tab_peak_rss_mib": observed_max,
        "headroom_mib": round(float(max_4tab_rss_mib) - observed_max, 1),
        "passed": passed,
    }


def run_smoke_suite(repo_root: Path, bench_tmp: Path, max_4tab_rss_mib: float) -> dict[str, Any]:
    fixtures = prepare_smoke_fixtures(bench_tmp / "smoke_fixtures")
    tasks = [
        (
            "open",
            "open_and_tiles",
            {
                "fixture_path": str(fixtures["ome_2d"]),
                "work_dir": str(bench_tmp / "w_smoke_open"),
                "n_cold": 1,
                "n_warm": 2,
                "n_burst": 2,
            },
        ),
        (
            "zstack",
            "zstack",
            {
                "fixture_path": str(fixtures["czi_zstack"]),
                "work_dir": str(bench_tmp / "w_smoke_z"),
                "n_jumps": 2,
                "n_steps": 2,
                "n_scrubs": 1,
                "scrub_steps": 6,
            },
        ),
        (
            "rt",
            "roundtrip",
            {
                "fixture_path": str(fixtures["lif_2d"]),
                "work_dir": str(bench_tmp / "w_smoke_rt"),
                "n_iters": 1,
            },
        ),
        (
            "mem4",
            "memory",
            {
                "mode": "four_concurrent",
                "fixture_entries": [
                    str(fixtures["ome_2d"]),
                    str(fixtures["czi_2d"]),
                    str(fixtures["lif_2d"]),
                    str(fixtures["czi_zstack"]),
                ],
                "work_dir": str(bench_tmp / "w_smoke_mem4"),
            },
        ),
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        fut_map = {
            name: pool.submit(run_child_scenario, repo_root, bench_tmp, scen, spec)
            for name, scen, spec in tasks
        }
        res_map = {name: fut.result() for name, fut in fut_map.items()}

    four_peaks = [float(res_map["mem4"]["rss"]["peak_rss_mib"])]
    return {
        "mode": "smoke",
        "open_and_tiles": {"smoke_ome_2d_L0": res_map["open"]},
        "zstack": {"smoke_czi_zstack": res_map["zstack"]},
        "roundtrip": {"smoke_lif_multiseries": res_map["rt"]},
        "memory": {"four_concurrent_slides_mib": summarize_mib(four_peaks)},
        "gate": _evaluate_gate(four_peaks, max_4tab_rss_mib),
    }


def run_standard_suite(
    repo_root: Path,
    docs_dir: Path | None,
    bench_tmp: Path,
    max_4tab_rss_mib: float,
) -> dict[str, Any]:
    fixtures = prepare_standard_fixtures(docs_dir, bench_tmp / "standard_fixtures")
    results: dict[str, Any] = {
        "mode": "standard",
        "fixture_sizes_mib": {
            k: round(p.stat().st_size / (1024.0 * 1024.0), 2) for k, p in fixtures.items()
        },
        "open_and_tiles": {},
        "zstack": {},
        "roundtrip": {},
        "memory": {},
    }

    open_targets: list[tuple[str, Path, int, int, int, int]] = []
    if "nd2_cell" in fixtures and "nd2_tissue" in fixtures:
        open_targets.extend(
            [
                ("nd2_cell_L0", fixtures["nd2_cell"], 0, 5, 12, 20),
                ("nd2_tissue_L0", fixtures["nd2_tissue"], 0, 5, 12, 20),
                ("nd2_tissue_sqlite_L1", fixtures["nd2_tissue"], 1, 1, 3, 20),
            ]
        )
    open_targets.extend(
        [
            ("czi_2d_L0", fixtures["czi_2d"], 0, 5, 12, 20),
            ("lif_multiseries_L0", fixtures["lif_2d"], 0, 5, 12, 20),
            ("ometiff_2d_L0", fixtures["ome_2d"], 0, 5, 12, 20),
            ("ometiff_large_72mib_untiled_L0", fixtures["ome_large_untiled"], 0, 5, 10, 15),
        ]
    )
    for key, fpath, lv, nc, nw, nb in open_targets:
        print(f"  [standard] open_and_tiles: {key} ...", flush=True)
        results["open_and_tiles"][key] = run_child_scenario(
            repo_root,
            bench_tmp,
            "open_and_tiles",
            {
                "fixture_path": str(fpath),
                "work_dir": str(bench_tmp / f"work_std_{key}"),
                "tile_level": lv,
                "n_cold": nc,
                "n_warm": nw,
                "n_burst": nb,
            },
        )

    for zkey, fpath in (
        ("czi_zstack", fixtures["czi_zstack"]),
        ("lif_zstack", fixtures["lif_zstack"]),
        ("ometiff_zstack", fixtures["ome_zstack"]),
    ):
        print(f"  [standard] zstack: {zkey} ...", flush=True)
        results["zstack"][zkey] = run_child_scenario(
            repo_root,
            bench_tmp,
            "zstack",
            {
                "fixture_path": str(fpath),
                "work_dir": str(bench_tmp / f"work_std_{zkey}"),
            },
        )

    rt_targets: list[tuple[str, Path]] = []
    if "nd2_cell" in fixtures:
        rt_targets.append(("nd2_cell", fixtures["nd2_cell"]))
    rt_targets.extend(
        [
            ("czi_2d", fixtures["czi_2d"]),
            ("lif_multiseries", fixtures["lif_2d"]),
            ("ometiff_2d", fixtures["ome_2d"]),
        ]
    )
    for rkey, fpath in rt_targets:
        print(f"  [standard] roundtrip: {rkey} ...", flush=True)
        results["roundtrip"][rkey] = run_child_scenario(
            repo_root,
            bench_tmp,
            "roundtrip",
            {
                "fixture_path": str(fpath),
                "work_dir": str(bench_tmp / f"work_std_rt_{rkey}"),
                "n_iters": 7,
            },
        )

    four_list = (
        [
            str(fixtures["nd2_cell"]),
            str(fixtures["nd2_tissue"]),
            str(fixtures["czi_2d"]),
            str(fixtures["czi_zstack"]),
        ]
        if "nd2_cell" in fixtures and "nd2_tissue" in fixtures
        else [
            str(fixtures["ome_2d"]),
            str(fixtures["lif_2d"]),
            str(fixtures["czi_2d"]),
            str(fixtures["czi_zstack"]),
        ]
    )
    single_target = fixtures.get("nd2_tissue", fixtures["ome_2d"])
    single_rss: list[float] = []
    four_rss: list[float] = []
    untiled_rss: list[float] = []
    for rep in range(5):
        r1 = run_child_scenario(
            repo_root,
            bench_tmp,
            "memory",
            {
                "mode": "single",
                "fixture_entries": [str(single_target)],
                "work_dir": str(bench_tmp / f"mem_std_1_{rep}"),
            },
        )
        single_rss.append(float(r1["rss"]["peak_rss_mib"]))
        r4 = run_child_scenario(
            repo_root,
            bench_tmp,
            "memory",
            {
                "mode": "four_concurrent",
                "fixture_entries": four_list,
                "work_dir": str(bench_tmp / f"mem_std_4_{rep}"),
            },
        )
        four_rss.append(float(r4["rss"]["peak_rss_mib"]))
        rl = run_child_scenario(
            repo_root,
            bench_tmp,
            "memory",
            {
                "mode": "large_untiled_ometiff",
                "fixture_entries": [str(fixtures["ome_large_untiled"])],
                "work_dir": str(bench_tmp / f"mem_std_u_{rep}"),
            },
        )
        untiled_rss.append(float(rl["rss"]["peak_rss_mib"]))

    results["memory"] = {
        "single_slide_mib": summarize_mib(single_rss),
        "four_concurrent_slides_mib": summarize_mib(four_rss),
        "large_72mib_untiled_ometiff_mib": summarize_mib(untiled_rss),
    }
    results["gate"] = _evaluate_gate(four_rss, max_4tab_rss_mib)
    return results


def run_scale_suite(repo_root: Path, bench_tmp: Path, max_4tab_rss_mib: float) -> dict[str, Any]:
    print("  [scale] Synthesizing multi-GB scale fixtures in isolated temp sandbox ...", flush=True)
    t_gen0 = time.perf_counter()
    fixtures, fixture_specs = prepare_scale_fixtures(bench_tmp / "scale_fixtures")
    gen_s = round(time.perf_counter() - t_gen0, 2)
    print(f"  [scale] Synthesized 4 multi-GB scale fixtures in {gen_s:.2f} s", flush=True)

    results: dict[str, Any] = {
        "mode": "scale",
        "fixture_generation_s": gen_s,
        "fixtures": fixture_specs,
        "combined_4tab_raw_mib": 4704.0,
        "open_and_tiles": {},
        "zstack": {},
        "roundtrip": {},
        "memory": {},
    }

    warm_czi_dir = bench_tmp / "warm_czi_mosaic_864mib"
    scale_open_targets = [
        ("ome_pyr_1536mib_L0", fixtures["ome_pyr_1536mib"], 3, 8, 15, None),
        ("ome_untiled_1536mib_L0", fixtures["ome_untiled_1536mib"], 3, 6, 12, None),
        ("czi_mosaic_864mib_L0", fixtures["czi_mosaic_864mib"], 3, 8, 15, warm_czi_dir),
    ]
    for key, fpath, nc, nw, nb, exp_warm in scale_open_targets:
        print(f"  [scale] open_and_tiles: {key} ...", flush=True)
        spec_obj: dict[str, Any] = {
            "fixture_path": str(fpath),
            "work_dir": str(bench_tmp / f"work_scale_{key}"),
            "tile_level": 0,
            "n_cold": nc,
            "n_warm": nw,
            "n_burst": nb,
        }
        if exp_warm is not None:
            spec_obj["export_warm_dir"] = str(exp_warm)
        results["open_and_tiles"][key] = run_child_scenario(
            repo_root, bench_tmp, "open_and_tiles", spec_obj, timeout=900
        )

    print("  [scale] zstack: czi_zstack_z32 (Z=32, 768 MiB raw) ...", flush=True)
    results["zstack"]["czi_zstack_z32"] = run_child_scenario(
        repo_root,
        bench_tmp,
        "zstack",
        {
            "fixture_path": str(fixtures["czi_zstack_z32"]),
            "work_dir": str(bench_tmp / "work_scale_z32"),
            "n_jumps": 12,
            "n_steps": 12,
            "n_scrubs": 5,
            "scrub_steps": 30,
        },
    )

    print("  [scale] roundtrip: ome_pyr_1536mib & czi_mosaic_864mib ...", flush=True)
    results["roundtrip"]["ome_pyr_1536mib"] = run_child_scenario(
        repo_root,
        bench_tmp,
        "roundtrip",
        {
            "fixture_path": str(fixtures["ome_pyr_1536mib"]),
            "work_dir": str(bench_tmp / "work_scale_rt_pyr"),
            "n_iters": 3,
        },
    )
    results["roundtrip"]["czi_mosaic_864mib"] = run_child_scenario(
        repo_root,
        bench_tmp,
        "roundtrip",
        {
            "fixture_path": str(fixtures["czi_mosaic_864mib"]),
            "work_dir": str(bench_tmp / "work_scale_rt_czi"),
            "prewarmed_dir": str(warm_czi_dir),
            "n_iters": 3,
        },
    )

    print("  [scale] clean-subprocess Peak RSS stress (1.5 GiB pyramidal, 1.5 GiB untiled, 864 MiB mosaic, 4-tab 4.59 GiB Compare) ...", flush=True)
    pyr_rss: list[float] = []
    untiled_rss: list[float] = []
    mosaic_warm_rss: list[float] = []
    four_tab_warm_rss: list[float] = []
    for rep in range(3):
        rp = run_child_scenario(
            repo_root,
            bench_tmp,
            "memory",
            {
                "mode": "single",
                "fixture_entries": [str(fixtures["ome_pyr_1536mib"])],
                "work_dir": str(bench_tmp / f"mem_scale_pyr_{rep}"),
            },
        )
        pyr_rss.append(float(rp["rss"]["peak_rss_mib"]))

        ru = run_child_scenario(
            repo_root,
            bench_tmp,
            "memory",
            {
                "mode": "large_untiled_ometiff",
                "fixture_entries": [str(fixtures["ome_untiled_1536mib"])],
                "work_dir": str(bench_tmp / f"mem_scale_untiled_{rep}"),
            },
        )
        untiled_rss.append(float(ru["rss"]["peak_rss_mib"]))

        rm = run_child_scenario(
            repo_root,
            bench_tmp,
            "memory",
            {
                "mode": "single",
                "fixture_entries": [
                    {
                        "path": str(fixtures["czi_mosaic_864mib"]),
                        "prewarmed_dir": str(warm_czi_dir),
                    }
                ],
                "work_dir": str(bench_tmp / f"mem_scale_mosaic_{rep}"),
            },
        )
        mosaic_warm_rss.append(float(rm["rss"]["peak_rss_mib"]))

        r4 = run_child_scenario(
            repo_root,
            bench_tmp,
            "memory",
            {
                "mode": "four_concurrent",
                "fixture_entries": [
                    str(fixtures["ome_pyr_1536mib"]),
                    str(fixtures["ome_untiled_1536mib"]),
                    {
                        "path": str(fixtures["czi_mosaic_864mib"]),
                        "prewarmed_dir": str(warm_czi_dir),
                    },
                    str(fixtures["czi_zstack_z32"]),
                ],
                "work_dir": str(bench_tmp / f"mem_scale_4tab_{rep}"),
            },
        )
        four_tab_warm_rss.append(float(r4["rss"]["peak_rss_mib"]))

    # Also measure 1 worst-case cold-build + 4-tab Compare in a single process (building 864 MiB .nd2svs in-process)
    r4_cold = run_child_scenario(
        repo_root,
        bench_tmp,
        "memory",
        {
            "mode": "four_concurrent",
            "fixture_entries": [
                str(fixtures["ome_pyr_1536mib"]),
                str(fixtures["ome_untiled_1536mib"]),
                str(fixtures["czi_mosaic_864mib"]),
                str(fixtures["czi_zstack_z32"]),
            ],
            "work_dir": str(bench_tmp / "mem_scale_4tab_coldbuild"),
        },
        timeout=900,
    )
    cold_4tab_peak = float(r4_cold["rss"]["peak_rss_mib"])

    results["memory"] = {
        "single_ome_pyr_1536mib_mib": summarize_mib(pyr_rss),
        "lazy_untiled_1536mib_ometiff_mib": summarize_mib(untiled_rss),
        "single_czi_mosaic_864mib_warm_mib": summarize_mib(mosaic_warm_rss),
        "czi_mosaic_864mib_cold_build_mib": results["open_and_tiles"]["czi_mosaic_864mib_L0"]["rss"]["peak_rss_mib"],
        "four_concurrent_large_slides_mib": summarize_mib(four_tab_warm_rss),
        "four_concurrent_including_cold_build_mib": round(cold_4tab_peak, 1),
    }
    results["gate"] = _evaluate_gate(four_tab_warm_rss, max_4tab_rss_mib)
    return results


def run_external_files_suite(
    repo_root: Path,
    input_files: list[Path],
    bench_tmp: Path,
    max_4tab_rss_mib: float,
    json_out: Path | None,
) -> dict[str, Any]:
    resolved_inputs: list[Path] = []
    pre_snapshots: dict[Path, dict[str, tuple[int, int]]] = {}
    json_out_parent = json_out.resolve().parent if json_out is not None else None

    for raw_p in input_files:
        p = raw_p.expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Benchmark input slide not found or not a regular file: {raw_p}")
        resolved_inputs.append(p)
        if p.parent != json_out_parent and p.parent not in pre_snapshots:
            pre_snapshots[p.parent] = _dir_snapshot(p.parent)

    results: dict[str, Any] = {
        "mode": "files",
        "files": [str(p) for p in resolved_inputs],
        "file_sizes_mib": {
            p.name: round(p.stat().st_size / (1024.0 * 1024.0), 2) for p in resolved_inputs
        },
        "open_and_tiles": {},
        "zstack": {},
        "roundtrip": {},
        "memory": {},
    }

    prewarmed_entries: list[dict[str, str]] = []
    single_rss_by_slide: dict[str, dict[str, Any]] = {}
    for idx, p in enumerate(resolved_inputs):
        key = f"{idx}_{p.name}"
        warm_dir = bench_tmp / f"warm_ext_{idx}"
        print(f"  [files] open_and_tiles: {p.name} ...", flush=True)
        open_res = run_child_scenario(
            repo_root,
            bench_tmp,
            "open_and_tiles",
            {
                "fixture_path": str(p),
                "work_dir": str(bench_tmp / f"work_ext_open_{idx}"),
                "tile_level": 0,
                "n_cold": 1,
                "n_warm": 5,
                "n_burst": 10,
                "export_warm_dir": str(warm_dir),
            },
            timeout=1800,
        )
        results["open_and_tiles"][key] = open_res
        if open_res.get("supported"):
            warm_slide = warm_dir / p.name
            local_src = warm_slide if warm_slide.is_file() else p
            if open_res.get("kind") == "plate":
                z_res = run_child_scenario(
                    repo_root,
                    bench_tmp,
                    "zstack",
                    {
                        "fixture_path": str(local_src),
                        "work_dir": str(bench_tmp / f"work_ext_z_{idx}"),
                        "n_jumps": 8,
                        "n_steps": 8,
                        "n_scrubs": 4,
                    },
                    timeout=900,
                )
                if z_res.get("supported"):
                    results["zstack"][key] = z_res

            rt_res = run_child_scenario(
                repo_root,
                bench_tmp,
                "roundtrip",
                {
                    "fixture_path": str(p),
                    "work_dir": str(bench_tmp / f"work_ext_rt_{idx}"),
                    "prewarmed_dir": str(warm_dir),
                    "n_iters": 3,
                },
                timeout=900,
            )
            results["roundtrip"][key] = rt_res

            r1 = run_child_scenario(
                repo_root,
                bench_tmp,
                "memory",
                {
                    "mode": "single",
                    "fixture_entries": [{"path": str(p), "prewarmed_dir": str(warm_dir)}],
                    "work_dir": str(bench_tmp / f"work_ext_mem1_{idx}"),
                },
                timeout=900,
            )
            single_rss_by_slide[key] = r1["rss"]

            if len(prewarmed_entries) < 4:
                prewarmed_entries.append({"path": str(p), "prewarmed_dir": str(warm_dir)})
            else:
                # Reclaim staged disk immediately beyond the 4-tab Compare set
                shutil.rmtree(warm_dir, ignore_errors=True)

    if not prewarmed_entries:
        raise RuntimeError("None of the supplied --files could be opened by nd2wsi")

    # Populate 4 distinct tabs (cycling across supplied slides when < 4 files are passed)
    four_entries = [prewarmed_entries[i % len(prewarmed_entries)] for i in range(4)]
    n_mem_reps = 3 if len(prewarmed_entries) >= 4 else 1
    four_peaks: list[float] = []
    for rep in range(n_mem_reps):
        r4 = run_child_scenario(
            repo_root,
            bench_tmp,
            "memory",
            {
                "mode": "four_concurrent",
                "fixture_entries": four_entries,
                "work_dir": str(bench_tmp / f"work_ext_mem4_{rep}"),
            },
            timeout=900,
        )
        four_peaks.append(float(r4["rss"]["peak_rss_mib"]))
    results["memory"]["single_slide_clean_rss"] = single_rss_by_slide
    results["memory"]["four_concurrent_slides_mib"] = summarize_mib(four_peaks)
    results["gate"] = _evaluate_gate(four_peaks, max_4tab_rss_mib)

    # Verify strict non-mutation of every source slide directory
    unchanged = True
    for folder, before in pre_snapshots.items():
        after = _dir_snapshot(folder)
        if after != before:
            unchanged = False
            raise RuntimeError(
                f"Source slide directory mutation detected in {folder}! "
                f"before={before!r}, after={after!r}"
            )
    results["source_dir_unchanged"] = unchanged
    return results


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def _git_short_sha(repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def _create_isolated_bench_tmp() -> tuple[Path, bool]:
    """Create a dedicated per-run temp directory inside ``$ND2WSI_BENCH_TMP`` or ``$TMPDIR``."""
    base_env = os.environ.get("ND2WSI_BENCH_TMP")
    if base_env:
        base = Path(base_env).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="nd2wsi_bench_run_", dir=str(base))), True
    return Path(tempfile.mkdtemp(prefix="nd2wsi_bench_run_")), True


def _run_child_entrypoint(args: argparse.Namespace) -> int:
    assert args.target_root is not None
    target_resolved = args.target_root.resolve()
    sys.path[:] = [
        str(target_resolved),
        *[
            p
            for p in sys.path
            if p
            and Path(p).resolve() != target_resolved
            and not (Path(p).resolve() / "nd2wsi" / "__init__.py").is_file()
        ],
    ]
    import nd2wsi

    actual_mod = Path(nd2wsi.__file__).resolve()
    if not actual_mod.is_relative_to(target_resolved):
        raise RuntimeError(
            f"Import isolation failure: expected {target_resolved}, got {actual_mod}"
        )
    spec = json.loads(args.child_spec.read_text(encoding="utf-8"))
    dispatch = {
        "open_and_tiles": child_open_and_tiles,
        "zstack": child_zstack_bench,
        "roundtrip": child_milestone_roundtrip,
        "memory": child_memory_stress,
    }
    res = dispatch[args.child_scenario](spec)
    res["verified_module_file"] = str(actual_mod)
    args.child_out.write_text(json.dumps(res, indent=2), encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Milestone 1 performance, multi-GB scale, and 4-tab RSS gate benchmark CLI."
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--suite",
        choices=("smoke", "standard", "scale"),
        default=None,
        help="Synthetic/isolated benchmark suite to execute (default: standard when --files is omitted).",
    )
    mode_group.add_argument(
        "--files",
        nargs="+",
        type=Path,
        help="External slide file(s) (.nd2, .czi, .lif, .ome.tif, .svs, .sdpc) to benchmark inside an isolated temp sandbox.",
    )
    parser.add_argument(
        "--max-4tab-rss-mib",
        type=float,
        default=DEFAULT_MAX_4TAB_RSS_MIB,
        help=f"Maximum allowed 4-concurrent-slide Peak RSS in MiB (default: {DEFAULT_MAX_4TAB_RSS_MIB} = 1.2 GiB).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Write machine-readable JSON benchmark report to this path.",
    )
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=None,
        help="Optional docs directory containing example_*.nd2 for --suite standard.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root containing the nd2wsi package to benchmark.",
    )
    # Internal child-process flags (hidden from user --help)
    parser.add_argument("--target-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--child-scenario", type=str, help=argparse.SUPPRESS)
    parser.add_argument("--child-spec", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--child-out", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.child_scenario:
        return _run_child_entrypoint(args)

    repo_root = args.repo_root.resolve()
    docs_dir = (args.docs_dir or (repo_root / "docs")).resolve()
    suite = args.suite or ("standard" if not args.files else None)
    max_rss = float(args.max_4tab_rss_mib)

    bench_tmp, should_cleanup = _create_isolated_bench_tmp()
    t_wall0 = time.perf_counter()
    try:
        if args.files:
            suite_res = run_external_files_suite(
                repo_root, args.files, bench_tmp, max_rss, args.json_out
            )
        elif suite == "smoke":
            suite_res = run_smoke_suite(repo_root, bench_tmp, max_rss)
        elif suite == "scale":
            suite_res = run_scale_suite(repo_root, bench_tmp, max_rss)
        else:
            suite_res = run_standard_suite(repo_root, docs_dir, bench_tmp, max_rss)
    finally:
        if should_cleanup:
            shutil.rmtree(bench_tmp, ignore_errors=True)

    wall_s = round(time.perf_counter() - t_wall0, 2)
    payload: dict[str, Any] = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_sha": _git_short_sha(repo_root),
        "repo_root": str(repo_root),
        "wall_seconds": wall_s,
        "temp_cleaned": not bench_tmp.exists(),
        "machine": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "results": suite_res,
    }

    if args.json_out is not None:
        out_path = args.json_out.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    gate = suite_res["gate"]
    status = "PASS" if gate["passed"] else "FAIL"
    print(
        f"[{status}] mode={suite_res['mode']} wall={wall_s:.2f}s "
        f"4tab_peak_rss={gate['observed_4tab_peak_rss_mib']:.1f} MiB "
        f"(limit={gate['max_4tab_rss_mib_limit']:.1f} MiB, headroom={gate['headroom_mib']:+.1f} MiB)",
        flush=True,
    )
    return 0 if gate["passed"] else EXIT_GATE_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
