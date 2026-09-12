"""Opt-in Metal reduction: safe fallback and exact native-value equivalence.

Hardware tests are deliberately not mocked: they may skip when the Metal
library/device is unavailable, but a CPU fallback after availability is a
failure. No acquisition or persistent cache is changed by these tests.
"""

import gc
import platform
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

from nd2wsi import metal


def _reference(block):
    """The existing box-mean-floor-v1 reducer, including ties-to-even."""
    source = block[np.newaxis, ...] if block.ndim == 2 else block
    c, h, w = source.shape
    source = source[:, : h // 2 * 2, : w // 2 * 2]
    result = np.rint(
        source.reshape(c, h // 2, 2, w // 2, 2).mean(axis=(2, 4), dtype=np.float32)
    ).astype(block.dtype)
    return result[0] if block.ndim == 2 else result


@pytest.fixture
def native_metal(monkeypatch):
    monkeypatch.setenv("ND2WSI_GPU_PYRAMID", "1")
    if not metal.available():
        pytest.skip("Native Metal library or Apple silicon GPU is unavailable")
    metal.reset_diagnostics()
    return metal


@pytest.mark.parametrize("setting", [None, "", "0", "false", "true", "yes", "2"])
def test_reduction_requires_explicit_opt_in(monkeypatch, setting):
    if setting is None:
        monkeypatch.delenv("ND2WSI_GPU_PYRAMID", raising=False)
    else:
        monkeypatch.setenv("ND2WSI_GPU_PYRAMID", setting)
    metal.reset_diagnostics()
    source = np.arange(64, dtype=np.uint16).reshape(8, 8)
    assert metal.reduce2x(source) is None
    assert metal.diagnostics()["gpu_calls"] == 0


@pytest.mark.parametrize(
    ("system", "machine"),
    [("win32", "ARM64"), ("linux", "aarch64"), ("darwin", "x86_64")],
)
def test_other_platforms_keep_cpu_fallback(monkeypatch, system, machine):
    monkeypatch.setenv("ND2WSI_GPU_PYRAMID", "1")
    monkeypatch.setattr(sys, "platform", system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    source = np.arange(64, dtype=np.uint16).reshape(8, 8)
    assert metal.reduce2x(source) is None


@pytest.mark.parametrize(
    "dtype", [np.bool_, np.int8, np.int16, np.uint32, np.float16, np.float32, np.float64]
)
def test_unsupported_dtype_falls_back_without_changing_input(monkeypatch, dtype):
    monkeypatch.setenv("ND2WSI_GPU_PYRAMID", "1")
    source = np.ones((2, 8, 8), dtype=dtype)
    before = source.copy()
    assert metal.reduce2x(source) is None
    np.testing.assert_array_equal(source, before)


def test_non_native_byte_order_is_not_reinterpreted(monkeypatch):
    monkeypatch.setenv("ND2WSI_GPU_PYRAMID", "1")
    dtype = np.dtype(np.uint16).newbyteorder("S")
    source = np.arange(64, dtype=np.uint16).astype(dtype).reshape(8, 8)
    before = source.tobytes()
    assert metal.reduce2x(source) is None
    assert source.tobytes() == before


@pytest.mark.parametrize(
    "shape",
    [(), (8,), (1, 1, 8, 8), (0, 8), (8, 0), (1, 8), (8, 1),
     (0, 8, 8), (2, 0, 8), (2, 8, 0), (2, 1, 8), (2, 8, 1)],
)
def test_unsupported_shape_and_collapsed_axes_fall_back(monkeypatch, shape):
    monkeypatch.setenv("ND2WSI_GPU_PYRAMID", "1")
    source = np.zeros(shape, dtype=np.uint16)
    assert metal.reduce2x(source) is None


def test_diagnostics_have_resettable_copy_and_gpu_counters():
    metal.reset_diagnostics()
    report = metal.diagnostics()
    for name in ("input_copy_bytes", "gpu_calls", "cpu_fallbacks", "gpu_seconds"):
        assert name in report
        assert report[name] == 0
    # Reports must be snapshots, not a mutable reference to live counters.
    report["gpu_calls"] = 123456
    assert metal.diagnostics()["gpu_calls"] == 0


def test_library_failure_warns_once_and_keeps_exact_cpu_fallback(monkeypatch):
    monkeypatch.setattr(metal, "enabled", lambda: True)
    monkeypatch.setattr(metal, "_warned", set())

    def unavailable():
        raise RuntimeError("synthetic unavailable Metal library")

    monkeypatch.setattr(metal, "_load_library", unavailable)
    metal.reset_diagnostics()
    source = np.arange(3 * 17 * 23, dtype=np.uint16).reshape(3, 17, 23)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        for _ in range(2):
            np.testing.assert_array_equal(metal.reduce2x_or_cpu(source), _reference(source))
    assert len(seen) == 1
    assert issubclass(seen[0].category, RuntimeWarning)
    assert "synthetic unavailable Metal library" in str(seen[0].message)
    assert metal.diagnostics()["cpu_fallbacks"] == 2
    assert metal.diagnostics()["gpu_calls"] == 0
    assert metal.diagnostics()["input_copy_bytes"] == 0


def test_native_error_does_not_return_uninitialized_pixels(monkeypatch):
    monkeypatch.setattr(metal, "enabled", lambda: True)
    monkeypatch.setattr(metal, "_warned", set())

    class FailedLibrary:
        def nd2wsi_metal_reduce(self, *args):
            args[-2].value = b"synthetic command buffer failure"
            return 1

    monkeypatch.setattr(metal, "_load_library", lambda: FailedLibrary())
    source = np.arange(64, dtype=np.uint16).reshape(1, 8, 8)
    with pytest.warns(RuntimeWarning, match="synthetic command buffer failure"):
        np.testing.assert_array_equal(metal.reduce2x_or_cpu(source), _reference(source))


def test_staging_budget_rejects_large_view_before_allocating(monkeypatch):
    monkeypatch.setattr(metal, "enabled", lambda: True)

    def must_not_load():
        pytest.fail("oversized staging must be rejected before loading or allocating")

    monkeypatch.setattr(metal, "_load_library", must_not_load)
    # A zero-stride view avoids actually allocating this 128 MiB input.
    source = np.broadcast_to(np.array(1, dtype=np.uint16), (8192, 8192))
    assert metal.reduce2x(source) is None


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
@pytest.mark.parametrize("shape", [(2, 2), (17, 23), (1, 64, 64), (3, 31, 45), (2, 256, 258)])
def test_native_random_even_and_odd_shapes_match_exactly(native_metal, dtype, shape):
    source = np.random.default_rng(126).integers(
        0, np.iinfo(dtype).max + 1, size=shape, dtype=dtype
    )
    before = source.copy()
    result = native_metal.reduce2x(source)
    assert result is not None, "Metal unexpectedly fell back after reporting availability"
    assert result.dtype == source.dtype
    np.testing.assert_array_equal(result, _reference(source))
    np.testing.assert_array_equal(source, before)
    assert not np.shares_memory(result, source)
    assert native_metal.diagnostics()["gpu_calls"] == 1


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
def test_native_extrema_and_ties_round_to_even(native_metal, dtype):
    high = np.iinfo(dtype).max
    # Each adjacent 2x2 block has mean 0, max, 0.5, 1.5, 2.5, max-0.5.
    source = np.array(
        [[0, 0, high, high, 0, 0, 1, 1, 2, 2, high, high],
         [0, 0, high, high, 1, 1, 2, 2, 3, 3, high - 1, high - 1]],
        dtype=dtype,
    )
    result = native_metal.reduce2x(source)
    assert result is not None
    np.testing.assert_array_equal(result, [[0, high, 0, 2, 2, high - 1]])
    np.testing.assert_array_equal(result, _reference(source))


@pytest.mark.parametrize("layout", ["strided", "transposed", "negative", "interleaved", "readonly"])
def test_native_noncontiguous_and_readonly_inputs(native_metal, layout):
    source = np.random.default_rng(10632).integers(
        0, 65536, size=(3, 34, 46), dtype=np.uint16
    )
    if layout == "strided":
        source = source[:, ::2, ::2]
    elif layout == "transposed":
        source = source.transpose(0, 2, 1)
    elif layout == "negative":
        source = source[::-1, ::-1, ::-1]
    elif layout == "interleaved":
        source = np.moveaxis(np.ascontiguousarray(np.moveaxis(source, 0, -1)), -1, 0)
    else:
        source.flags.writeable = False
    before = source.copy()
    result = native_metal.reduce2x(source)
    assert result is not None
    np.testing.assert_array_equal(result, _reference(source))
    np.testing.assert_array_equal(source, before)


def test_native_output_lifetime_survives_input_and_subsequent_calls(native_metal):
    source = np.arange(3 * 64 * 66, dtype=np.uint16).reshape(3, 64, 66)
    expected = _reference(source)
    result = native_metal.reduce2x(source)
    assert result is not None
    source.fill(0)
    del source
    gc.collect()
    for value in range(8):
        later = native_metal.reduce2x(np.full((3, 64, 66), value, dtype=np.uint16))
        assert later is not None
        np.testing.assert_array_equal(later, value)
    np.testing.assert_array_equal(result, expected)


def test_native_concurrent_calls_are_independent_and_accounted(native_metal):
    def reduce(seed):
        source = np.random.default_rng(seed).integers(
            0, 65536, size=(2, 129, 131), dtype=np.uint16
        )
        result = native_metal.reduce2x(source)
        assert result is not None
        np.testing.assert_array_equal(result, _reference(source))
        return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reduce, range(16)))
    assert len(results) == 16
    assert native_metal.diagnostics()["gpu_calls"] == 16
    assert native_metal.diagnostics()["gpu_seconds"] >= 0


def test_native_reports_input_staging_without_claiming_zero_copy(native_metal):
    source = np.arange(64 * 64, dtype=np.uint16).reshape(64, 64)
    result = native_metal.reduce2x(source)
    assert result is not None
    report = native_metal.diagnostics()
    # Phase 1 stages input into a Metal shared buffer; UMA is not itself
    # proof that this implementation performs no system-memory copy.
    assert report["input_copy_bytes"] >= source.nbytes
    assert report["gpu_calls"] == 1
    native_metal.reset_diagnostics()
    assert native_metal.diagnostics()["input_copy_bytes"] == 0
    assert native_metal.diagnostics()["gpu_calls"] == 0


def _build_test_pyramid(directory, values):
    """Exercise both production write paths through all collapsed edge levels."""
    import dask.array as da

    from nd2wsi.convert import _downsample_into, _write_level1_from_source
    from nd2wsi.storage import ZarrV2Storage

    storage = ZarrV2Storage()
    root = storage.create_group(directory)
    root.attrs.update({"test_metadata": {"pixel_size_um": 0.5, "preserve": True}})
    source = SimpleNamespace(
        data=da.from_array(values, chunks=(1, 7, 11)),
        shape=values.shape,
        dtype=values.dtype,
    )
    c, h, w = values.shape
    first = storage.create_array(root, "1", (c, h // 2, w // 2), values.dtype, tile=4)
    _write_level1_from_source(source, first, storage)
    levels = [np.asarray(first[:])]
    previous = first
    while previous.shape[-2:] != (1, 1):
        c, h, w = previous.shape
        shape = (c, max(1, h // 2), max(1, w // 2))
        target = storage.create_array(root, str(len(levels) + 1), shape, values.dtype, tile=4)
        _downsample_into(previous, target, 4, values.dtype, storage)
        levels.append(np.asarray(target[:]))
        previous = target
    assert "0" not in root
    assert dict(root.attrs) == {"test_metadata": {"pixel_size_um": 0.5, "preserve": True}}
    return levels


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
def test_native_full_pyramid_storage_matches_cpu_with_sparse_odd_and_collapsed_edges(
    native_metal, monkeypatch, tmp_path, dtype
):
    source = np.random.default_rng(10686).integers(
        0, np.iinfo(dtype).max + 1, size=(2, 33, 67), dtype=dtype
    )
    source[:, :16, :16] = 0
    source[1] = 0  # An entire sparse channel must remain omitted at every level.
    monkeypatch.setenv("ND2WSI_GPU_PYRAMID", "0")
    expected = _build_test_pyramid(tmp_path / "cpu", source)
    monkeypatch.setenv("ND2WSI_GPU_PYRAMID", "1")
    native_metal.reset_diagnostics()
    actual = _build_test_pyramid(tmp_path / "metal", source)
    assert len(actual) == len(expected) == 6
    for reference, result in zip(expected, actual, strict=True):
        np.testing.assert_array_equal(result, reference)
        assert not result[1].any()
    report = native_metal.diagnostics()
    assert report["gpu_calls"] > 0
    assert report["cpu_fallbacks"] == 0
    # Identical payloads and chunk omission also preserve the storage layout.
    cpu_files = {
        path.relative_to(tmp_path / "cpu"): path.read_bytes()
        for path in (tmp_path / "cpu").rglob("*") if path.is_file()
    }
    metal_files = {
        path.relative_to(tmp_path / "metal"): path.read_bytes()
        for path in (tmp_path / "metal").rglob("*") if path.is_file()
    }
    assert metal_files == cpu_files


def test_full_pyramid_recovers_from_unavailable_library(monkeypatch, tmp_path):
    source = np.random.default_rng(10686).integers(
        0, 65536, size=(2, 33, 67), dtype=np.uint16
    )
    monkeypatch.setattr(metal, "enabled", lambda: False)
    expected = _build_test_pyramid(tmp_path / "cpu", source)
    monkeypatch.setattr(metal, "enabled", lambda: True)
    monkeypatch.setattr(metal, "_warned", set())

    def unavailable():
        raise RuntimeError("synthetic unavailable integration library")

    monkeypatch.setattr(metal, "_load_library", unavailable)
    metal.reset_diagnostics()
    with pytest.warns(RuntimeWarning, match="synthetic unavailable integration library") as seen:
        actual = _build_test_pyramid(tmp_path / "fallback", source)
    assert len(seen) == 1
    for reference, result in zip(expected, actual, strict=True):
        np.testing.assert_array_equal(result, reference)
    assert metal.diagnostics()["gpu_calls"] == 0
    assert metal.diagnostics()["cpu_fallbacks"] > 0
