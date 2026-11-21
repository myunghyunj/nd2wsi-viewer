"""Regression tests for the flat-module split of nd2wsi.ometiff."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import numpy as np
import pytest
import tifffile

from nd2wsi import ometiff, ometiff_levels, ometiff_metadata, ometiff_stack, plate


def test_ometiff_remains_flat_module_with_flat_siblings_and_no_tkinter() -> None:
    nd2wsi_dir = Path(ometiff.__file__).resolve().parent
    assert Path(ometiff.__file__).name == "ometiff.py"
    assert not hasattr(ometiff, "__path__")

    for mod_name in ("ometiff.py", "ometiff_metadata.py", "ometiff_levels.py", "ometiff_stack.py"):
        mod_path = nd2wsi_dir / mod_name
        assert mod_path.is_file(), f"expected flat sibling module {mod_name}"
        tree = ast.parse(mod_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(("tkinter", "_tkinter"))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith(("tkinter", "_tkinter"))


def test_ometiff_facade_reexports_preserve_object_identity() -> None:
    # Metadata symbols
    for name in (
        "OMETIFF_SUFFIXES",
        "is_ometiff",
        "_parse_ome_xml",
        "_parse_ome_color",
        "_parse_qpi_page_meta",
        "_normalize_dtype",
        "_ParsedSeriesLayout",
        "_parse_series_axes",
        "_resolve_metadata_for_series",
        "_apply_col_override",
    ):
        assert getattr(ometiff, name) is getattr(ometiff_metadata, name)

    # Level / pyramid symbols
    for name in (
        "UNTILED_MAX_PIXELS",
        "UNTILED_EAGER_MAX_BYTES",
        "_OmeTiledLevel",
        "_CoarseMemoLevel",
        "_MemoryPyramidLevel",
        "_LazyUntiledLevel",
        "_SyntheticPageSeries",
        "_group_logical_series",
        "_series_2d_to_cyx",
        "_tiled_level_reader_for",
        "_untiled_window_reader_for",
        "_compute_default_windows",
    ):
        assert getattr(ometiff, name) is getattr(ometiff_levels, name)

    # Stack symbols & shared caches
    for name in (
        "STACK_MIN_DIM",
        "STACK_MAX_DIM",
        "STACK_MAX_FRAME_PX",
        "PLATE_K",
        "THUMB_K",
        "ContainerSceneStackSource",
        "_BaseInteractiveStackSource",
        "_StackArrayLevel",
        "_StackReducedLevel",
        "_sharpness_score",
        "_STACK_FRAME_CACHE",
        "_STACK_REDUCED_CACHE",
    ):
        assert getattr(ometiff, name) is getattr(ometiff_stack, name)

    assert ometiff._STACK_FRAME_CACHE is plate._FRAME_CACHE
    assert ometiff.os is os


def test_ometiff_facade_monkeypatch_hooks_remain_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arr = np.arange(2 * 64 * 64, dtype=np.uint16).reshape(2, 64, 64)
    tif_path = tmp_path / "hook_2d.ome.tif"
    tifffile.imwrite(tif_path, arr, ome=True, photometric="minisblack", metadata={"axes": "CYX"})

    # 1. Monkeypatching ometiff.UNTILED_EAGER_MAX_BYTES switches open_direct to lazy streaming
    monkeypatch.setattr(ometiff, "UNTILED_EAGER_MAX_BYTES", 1)
    opened_paths: list[Path] = []
    real_open = ometiff.os.open

    def tracking_open(p, flags, *args, **kwargs):
        fd = real_open(p, flags, *args, **kwargs)
        opened_paths.append(Path(p).resolve())
        return fd

    monkeypatch.setattr(ometiff.os, "open", tracking_open)

    def forbid_eager(*_args, **_kwargs):
        raise AssertionError("_series_2d_to_cyx must not be called when lazy-streaming")

    monkeypatch.setattr(ometiff, "_series_2d_to_cyx", forbid_eager)
    root, _attrs = ometiff.open_direct(tif_path, tile=32)
    try:
        assert isinstance(root["0"], ometiff._LazyUntiledLevel)
        assert tif_path.resolve() in opened_paths
        np.testing.assert_array_equal(root["0"][:, :, :], arr)
    finally:
        root.close(delay=0)

    # 2. Monkeypatching ometiff._series_2d_to_cyx intercepts eager open_direct AND OmeTiffStackSource
    calls: list[tuple[int, int]] = []
    real_decode = ometiff_levels._series_2d_to_cyx

    def spy_decode(series, layout, *, z_idx=0, t_idx=0):
        calls.append((z_idx, t_idx))
        return real_decode(series, layout, z_idx=z_idx, t_idx=t_idx)

    monkeypatch.setattr(ometiff, "UNTILED_EAGER_MAX_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(ometiff, "_series_2d_to_cyx", spy_decode)

    root_eager, _ = ometiff.open_direct(tif_path, tile=32)
    try:
        assert isinstance(root_eager["0"], ometiff._MemoryPyramidLevel)
        assert calls == [(0, 0)]
    finally:
        root_eager.close(delay=0)

    stack_arr = np.arange(3 * 2 * 32 * 32, dtype=np.uint16).reshape(3, 2, 32, 32)
    stack_path = tmp_path / "hook_stack.ome.tif"
    tifffile.imwrite(
        stack_path, stack_arr, ome=True, photometric="minisblack", metadata={"axes": "ZCYX"}
    )
    calls.clear()
    src = ometiff.OmeTiffStackSource(stack_path, tile=32)
    try:
        plane = src.frame(0, 0, 2)
        np.testing.assert_array_equal(plane, stack_arr[2])
        assert (2, 0) in calls
    finally:
        src.close(timeout=5.0)
