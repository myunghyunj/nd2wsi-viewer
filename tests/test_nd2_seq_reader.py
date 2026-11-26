"""Unit and edge-case tests for sequential ND2 Level-1 layout probing and streaming."""

from __future__ import annotations

import mmap
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import dask
import dask.array as da
import numpy as np
import pytest

from nd2wsi import convert_cache as cc
from nd2wsi.reader import (
    ChannelInfo,
    PlaneSelection,
    PlaneSource,
    SequentialNd2Layout,
    level_shapes,
    sequential_nd2_layout,
)
from nd2wsi.storage import DEFAULT_STORAGE
from nd2wsi.storage.single_file_writer import _SingleFileStagingBackend


class _FakeMmapND2:
    """Synthetic uncompressed mmap-backed ND2File matching nd2's read_frame contract."""

    def __init__(
        self,
        path: Path,
        data_cyx: np.ndarray,
        *,
        header_bytes: int = 128,
        row_pad_bytes: int = 0,
        trailing_bytes: int = 0,
        is_legacy: bool = False,
        compression: str = "none",
        n_z: int = 1,
        planar: bool = False,
        extra_x_pad_bytes: int = 0,
        copy_frame: bool = False,
    ) -> None:
        self.path = path
        self.is_legacy = is_legacy
        self.attributes = SimpleNamespace(compressionType=compression)
        c, h, w = data_cyx.shape
        self.dtype = data_cyx.dtype
        self.sizes = {"Y": h, "X": w}
        if c > 1:
            self.sizes["C"] = c
        if n_z > 1:
            self.sizes["Z"] = n_z
        self._c = c
        self._h = h
        self._w = w
        self._planar = planar
        self._copy_frame = copy_frame

        itemsize = data_cyx.dtype.itemsize
        if planar:
            raw_frame = np.ascontiguousarray(data_cyx).tobytes()
            self._x_stride = itemsize
            self._y_stride = w * itemsize
            self._c_stride = h * w * itemsize
        else:
            x_stride = c * itemsize + extra_x_pad_bytes
            y_stride = w * x_stride + row_pad_bytes
            self._x_stride = x_stride
            self._y_stride = y_stride
            self._c_stride = itemsize
            # Write exact frame bytes including any end-of-row padding (except after last row)
            frame_bytes = (h - 1) * y_stride + w * x_stride
            buf = bytearray(frame_bytes)
            interleaved = np.ascontiguousarray(data_cyx.transpose(1, 2, 0))
            for row in range(h):
                row_off = row * y_stride
                if extra_x_pad_bytes == 0:
                    row_raw = interleaved[row].tobytes()
                    buf[row_off : row_off + len(row_raw)] = row_raw
                else:
                    for col in range(w):
                        pix_off = row_off + col * x_stride
                        pix_raw = interleaved[row, col].tobytes()
                        buf[pix_off : pix_off + len(pix_raw)] = pix_raw
            raw_frame = bytes(buf)

        header = bytes((i * 37) % 256 for i in range(header_bytes))
        tail = b"\xaa" * trailing_bytes
        path.write_bytes(header + raw_frame + tail)
        self._header_bytes = header_bytes
        self._fh = open(path, "rb")
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)

    def read_frame(self, _seq: int) -> np.ndarray:
        c, h, w = self._c, self._h, self._w
        if self._planar:
            arr = np.ndarray(
                shape=(c, h, w) if c > 1 else (h, w),
                dtype=self.dtype,
                buffer=self._mm,
                offset=self._header_bytes,
                strides=(self._c_stride, self._y_stride, self._x_stride)
                if c > 1
                else (self._y_stride, self._x_stride),
            )
        else:
            if c == 1:
                arr = np.ndarray(
                    shape=(h, w),
                    dtype=self.dtype,
                    buffer=self._mm,
                    offset=self._header_bytes,
                    strides=(self._y_stride, self._x_stride),
                )
            else:
                arr = np.ndarray(
                    shape=(c, h, w),
                    dtype=self.dtype,
                    buffer=self._mm,
                    offset=self._header_bytes,
                    strides=(self._c_stride, self._y_stride, self._x_stride),
                )
        return arr.copy() if self._copy_frame else arr

    def close(self) -> None:
        self._mm.close()
        self._fh.close()

    def __enter__(self) -> _FakeMmapND2:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _compare_seq_vs_dask(
    tmp_path: Path,
    data_cyx: np.ndarray,
    *,
    tile: int,
    workers: int = 4,
    row_pad_bytes: int = 0,
    trailing_bytes: int = 0,
    selection: PlaneSelection | None = None,
    use_single_file: bool = True,
) -> list[float]:
    path = tmp_path / "synth.nd2"
    sel = selection or PlaneSelection()
    c, h, w = data_cyx.shape
    l1_shape = (c, h // 2, w // 2)
    fractions: list[float] = []

    with ExitStack() as stack:
        fake = stack.enter_context(
            _FakeMmapND2(
                path,
                data_cyx,
                row_pad_bytes=row_pad_bytes,
                trailing_bytes=trailing_bytes,
            )
        )
        layout = sequential_nd2_layout(fake, sel, path, stack=stack)
        assert layout is not None

        # Reference via Dask _write_level1_from_source
        src = PlaneSource(
            data=da.from_array(data_cyx, chunks=(1, tile * 2, tile * 2)),
            dtype=data_cyx.dtype,
            shape=(c, h, w),
            rgb=False,
            channels=[ChannelInfo(name=f"c{i}", color=(255, 255, 255)) for i in range(c)],
            pixel_size_um=None,
            source_name=path.name,
            selection={"t": 0, "p": 0, "z": 0},
            notes=[],
        )
        ref_root = DEFAULT_STORAGE.create_group(tmp_path / "ref.ome.zarr")
        ref_arr = DEFAULT_STORAGE.create_array(ref_root, "1", l1_shape, data_cyx.dtype, tile)
        with dask.config.set(scheduler="threads", num_workers=workers):
            cc._write_level1_from_source(src, ref_arr, DEFAULT_STORAGE)
        ref_l1 = np.asarray(ref_arr[:])

        # Candidate via _write_level1_sequential_nd2
        if use_single_file:
            backend = _SingleFileStagingBackend()
            cand_root = backend.create_group(tmp_path / "cand.nd2svs.building-1")
            cand_arr = backend.create_array(cand_root, "1", l1_shape, data_cyx.dtype, tile)
            ok = cc._write_level1_sequential_nd2(
                layout,
                cand_arr,
                backend,
                workers=workers,
                on_fraction=fractions.append,
            )
            assert ok is True
            cand_root.flush()
            cand_root.assert_all_coords_accounted()
            cand_l1 = cand_arr[:]
            cand_root.close_handles()
        else:
            cand_root = DEFAULT_STORAGE.create_group(tmp_path / "cand.ome.zarr")
            cand_arr = DEFAULT_STORAGE.create_array(cand_root, "1", l1_shape, data_cyx.dtype, tile)
            ok = cc._write_level1_sequential_nd2(
                layout,
                cand_arr,
                DEFAULT_STORAGE,
                workers=workers,
                on_fraction=fractions.append,
            )
            assert ok is True
            cand_l1 = np.asarray(cand_arr[:])

        np.testing.assert_array_equal(cand_l1, ref_l1)
    return fractions


@pytest.mark.parametrize(
    "shape,dtype,tile,workers,row_pad",
    [
        ((1, 64, 64), np.uint8, 32, 4, 0),  # C == 1 (c_stride == 0), uint8
        ((1, 65, 65), np.uint16, 32, 4, 2),  # C == 1, odd dims + row padding
        ((2, 68, 70), np.uint16, 32, 4, 4),  # exact-EOF padded even H, partial final band
        ((3, 66, 10), np.uint16, 32, 8, 2),  # W*x_stride = 60 < 64, l1_w=5 < workers=8
        ((4, 131, 129), np.uint16, 32, 4, 8),  # H=2*tile*2+3, W odd, 4-ch padded
        ((8, 72, 80), np.uint8, 32, 4, 0),  # 8-channel uint8
    ],
)
def test_sequential_nd2_matches_dask_across_geometries_and_padding(
    tmp_path, shape, dtype, tile, workers, row_pad
):
    rng = np.random.default_rng(42)
    hi = 255 if dtype == np.uint8 else 65535
    data = rng.integers(0, hi, size=shape, dtype=dtype)
    if shape[0] >= 2:
        data[1] = 0  # exercise all-zero chunks alongside non-zero channels
    fractions = _compare_seq_vs_dask(
        tmp_path,
        data,
        tile=tile,
        workers=workers,
        row_pad_bytes=row_pad,
        trailing_bytes=0,  # exact EOF right after final valid pixel
    )
    assert fractions and fractions[-1] == pytest.approx(1.0)
    assert all(fractions[i] <= fractions[i + 1] for i in range(len(fractions) - 1))


def test_sequential_nd2_non_dividing_strip_rows_and_multipass_cgroup(tmp_path, monkeypatch):
    # Force strip_rows=6 (does not divide tile*2=32) AND multi-pass c_group (C=8 -> 2 passes of 4)
    rng = np.random.default_rng(77)
    data = rng.integers(0, 4096, size=(8, 98, 66), dtype=np.uint16)
    # y_stride = 66 * 8 * 2 = 1056 bytes; 6 * 1056 = 6336 -> strip_rows = 6
    monkeypatch.setattr(cc, "_SEQ_STRIP_BUDGET_BYTES", 7000)
    # one_ch_band_bytes = 16 * 33 * 2 = 1056 bytes; budget=5500 -> c_group_max=5 -> 2 passes of 4!
    monkeypatch.setattr(cc, "_SEQ_BAND_BUDGET_BYTES", 5500)
    fractions = _compare_seq_vs_dask(tmp_path, data, tile=16, workers=4)
    # ny_tiles = ceil(49 / 16) = 4, n_passes = 2 -> 8 monotonic progress steps
    assert len(fractions) == 8
    assert fractions[-1] == pytest.approx(1.0)
    assert all(fractions[i] < fractions[i + 1] for i in range(len(fractions) - 1))


def test_sequential_nd2_z_max_single_plane_allowed_multi_z_rejected(tmp_path):
    rng = np.random.default_rng(11)
    data = rng.integers(0, 1000, size=(2, 32, 32), dtype=np.uint16)
    # Z == 1 with selection.z == "max" is eligible and bit-exact
    _compare_seq_vs_dask(tmp_path, data, tile=16, selection=PlaneSelection(z="max"))

    # Z > 1 with selection.z == "max" fails closed to None
    p2 = tmp_path / "multi_z.nd2"
    with _FakeMmapND2(p2, data, n_z=3) as fake_multi:
        assert sequential_nd2_layout(fake_multi, PlaneSelection(z="max"), p2) is None


def test_sequential_nd2_fail_closed_fallbacks(tmp_path, monkeypatch):
    rng = np.random.default_rng(3)
    u16 = rng.integers(0, 1000, size=(2, 32, 32), dtype=np.uint16)
    sel = PlaneSelection()

    # Compressed ND2
    with _FakeMmapND2(tmp_path / "comp.nd2", u16, compression="lossless") as f:
        assert sequential_nd2_layout(f, sel, f.path) is None

    # Legacy ND2
    with _FakeMmapND2(tmp_path / "legacy.nd2", u16, is_legacy=True) as f:
        assert sequential_nd2_layout(f, sel, f.path) is None

    # float32 dtype
    f32 = rng.random((2, 32, 32), dtype=np.float32)
    with _FakeMmapND2(tmp_path / "f32.nd2", f32) as f:
        assert sequential_nd2_layout(f, sel, f.path) is None

    # Non-native endianness
    be = u16.astype(">u2")
    with _FakeMmapND2(tmp_path / "be.nd2", be) as f:
        assert sequential_nd2_layout(f, sel, f.path) is None

    # Planar strides
    with _FakeMmapND2(tmp_path / "planar.nd2", u16, planar=True) as f:
        assert sequential_nd2_layout(f, sel, f.path) is None

    # Pixel-padded x_stride > C * itemsize
    with _FakeMmapND2(tmp_path / "xpad.nd2", u16, extra_x_pad_bytes=2) as f:
        assert sequential_nd2_layout(f, sel, f.path) is None

    # 2 * y_stride > 48 MiB limit
    with _FakeMmapND2(tmp_path / "wide.nd2", u16) as f:
        monkeypatch.setattr("nd2wsi.reader._SEQ_MAX_STRIP_BYTES", 64)
        assert sequential_nd2_layout(f, sel, f.path) is None
    monkeypatch.setattr("nd2wsi.reader._SEQ_MAX_STRIP_BYTES", 48 * 1024 * 1024)

    # Probe mismatch (corrupt file bytes after mmap via patched read_at)
    with _FakeMmapND2(tmp_path / "badprobe.nd2", u16) as f:
        monkeypatch.setattr("nd2wsi.reader._read_at_exact", lambda _fd, n, _o: b"\x00" * n)
        assert sequential_nd2_layout(f, sel, f.path) is None
    monkeypatch.undo()

    # c_group_max == 0 (even 1 channel exceeds band budget) -> returns False
    with _FakeMmapND2(tmp_path / "tinybudget.nd2", u16) as f:
        layout = sequential_nd2_layout(f, sel, f.path)
        assert layout is not None
        root = DEFAULT_STORAGE.create_group(tmp_path / "tb.ome.zarr")
        arr = DEFAULT_STORAGE.create_array(root, "1", (2, 16, 16), u16.dtype, 16)
        monkeypatch.setattr(cc, "_SEQ_BAND_BUDGET_BYTES", 10)
        assert cc._write_level1_sequential_nd2(layout, arr, DEFAULT_STORAGE, workers=2) is False
    monkeypatch.undo()

    # Mismatched target array shape / dtype / chunk_shape -> returns False
    with _FakeMmapND2(tmp_path / "mismatch.nd2", u16) as f:
        layout = sequential_nd2_layout(f, sel, f.path)
        assert layout is not None
        root = DEFAULT_STORAGE.create_group(tmp_path / "mm.ome.zarr")
        bad_arr = DEFAULT_STORAGE.create_array(root, "1", (2, 8, 8), u16.dtype, 16)
        assert cc._write_level1_sequential_nd2(layout, bad_arr, DEFAULT_STORAGE, workers=2) is False


def test_short_os_read_retry_and_error_teardown_no_deadlock(tmp_path, monkeypatch):
    rng = np.random.default_rng(19)
    data = rng.integers(1, 4096, size=(2, 64, 64), dtype=np.uint16)
    path = tmp_path / "short_read.nd2"

    # 1. Short os.read (cap every read to 37 bytes) still produces bit-exact output
    real_read = os.read
    ok_short = tmp_path / "ok_short"
    ok_short.mkdir()
    monkeypatch.setattr(os, "read", lambda fd, n: real_read(fd, min(n, 37)))
    _compare_seq_vs_dask(ok_short, data, tile=16, workers=2)
    monkeypatch.undo()

    # 2. Reader-side OSError propagates promptly without hanging consumer
    with _FakeMmapND2(path, data) as fake:
        layout = sequential_nd2_layout(fake, PlaneSelection(), path)
        assert layout is not None
        root = DEFAULT_STORAGE.create_group(tmp_path / "err_r.ome.zarr")
        arr = DEFAULT_STORAGE.create_array(root, "1", (2, 32, 32), data.dtype, 16)
        read_calls = 0

        def fail_read(fd, n):
            nonlocal read_calls
            read_calls += 1
            if read_calls == 2:
                raise OSError("simulated USB disconnect")
            return real_read(fd, n)

        monkeypatch.setattr(os, "read", fail_read)
        monkeypatch.setattr(cc, "_SEQ_STRIP_BUDGET_BYTES", 512)
        with pytest.raises(OSError, match="USB disconnect"):
            cc._write_level1_sequential_nd2(layout, arr, DEFAULT_STORAGE, workers=2)
        monkeypatch.undo()

        # 3. Consumer/writer-side error while free_slots is empty exits cleanly (no reader hang)
        class _FailChunkArray:
            chunk_shape = (1, 16, 16)
            shape = (2, 32, 32)
            dtype = data.dtype

            def write(self, _index, _sub):
                raise RuntimeError("simulated disk full during band encode")

        class _FailStorage:
            def chunk_array(self, _arr):
                return _FailChunkArray()

        monkeypatch.setattr(cc, "_SEQ_STRIP_BUDGET_BYTES", 512)
        with pytest.raises(RuntimeError, match="disk full"):
            cc._write_level1_sequential_nd2(
                layout,
                SimpleNamespace(shape=(2, 32, 32), dtype=data.dtype),
                _FailStorage(),  # type: ignore[arg-type]
                workers=2,
            )


def test_sequential_nd2_invokes_reduce2x_or_cpu_for_metal_compatibility(tmp_path, monkeypatch):
    from nd2wsi import metal as metal_mod

    rng = np.random.default_rng(55)
    data = rng.integers(0, 1000, size=(2, 32, 32), dtype=np.uint16)
    calls = 0
    real_reduce = metal_mod.reduce2x_or_cpu

    def spy_reduce(arr, out_dtype=None):
        nonlocal calls
        calls += 1
        return real_reduce(arr, out_dtype)

    monkeypatch.setattr(metal_mod, "reduce2x_or_cpu", spy_reduce)
    _compare_seq_vs_dask(tmp_path, data, tile=16, workers=2)
    assert calls > 0


@pytest.mark.realdata
@pytest.mark.parametrize("fixture", ["docs/example_tissue.nd2", "docs/example_cell.nd2"])
def test_sequential_nd2_matches_dask_on_real_nd2_fixtures(tmp_path, fixture):
    import nd2

    nd2_path = Path(fixture)
    if not nd2_path.is_file():
        pytest.skip(f"missing fixture {fixture}")
    sel = PlaneSelection()
    tile = 256
    with ExitStack() as stack:
        f = stack.enter_context(nd2.ND2File(str(nd2_path)))
        layout = sequential_nd2_layout(f, sel, nd2_path, stack=stack)
        assert isinstance(layout, SequentialNd2Layout)
        src = cc._open_conversion_source(stack, nd2_path, sel, tile, overview=True)
        shapes = level_shapes(src.shape[1], src.shape[2], tile)
        l1_shape = (src.shape[0], shapes[1][0], shapes[1][1])

        ref_root = DEFAULT_STORAGE.create_group(tmp_path / "ref.ome.zarr")
        ref_arr = DEFAULT_STORAGE.create_array(ref_root, "1", l1_shape, src.dtype, tile)
        with dask.config.set(scheduler="threads", num_workers=4):
            cc._write_level1_from_source(src, ref_arr, DEFAULT_STORAGE)

        backend = _SingleFileStagingBackend()
        cand_root = backend.create_group(tmp_path / "cand.nd2svs.building-real")
        cand_arr = backend.create_array(cand_root, "1", l1_shape, src.dtype, tile)
        assert cc._write_level1_sequential_nd2(layout, cand_arr, backend, workers=4) is True
        cand_root.flush()
        cand_root.assert_all_coords_accounted()
        np.testing.assert_array_equal(cand_arr[:], np.asarray(ref_arr[:]))
        cand_root.close_handles()
