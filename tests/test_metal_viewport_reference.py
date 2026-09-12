"""Independent display-contract and benchmark-report tests.

Pixel reference tests target the production CPU display function *before JPEG*.
An offscreen GPU readback, when added below, is a correctness-only test facility;
it must never be confused with the actual drawable presentation path.
"""

from __future__ import annotations

import copy
import ctypes
import importlib.util
import platform
from pathlib import Path

import numpy as np
import pytest

from nd2wsi.render import composite

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_metal_viewport.py"
_SPEC = importlib.util.spec_from_file_location("viewport_benchmark", _SCRIPT)
benchmark = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark)


@pytest.fixture(scope="module")
def gpu_composite():
    """Test-only CPU readback from the real production fragment pipeline."""
    path = _SCRIPT.parent.parent / "nd2wsi" / "metal_viewport" / "libnd2wsi_viewport.dylib"
    if platform.system() != "Darwin" or platform.machine() != "arm64" or not path.is_file():
        pytest.skip("requires built native viewport on Apple silicon")
    library = ctypes.CDLL(str(path))
    function = library.nd2wsi_viewport_composite
    function.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    function.restype = ctypes.c_int

    def invoke(raw, windows, gammas, colors, visible):
        raw = np.ascontiguousarray(raw, dtype=np.uint16)
        windows = np.ascontiguousarray(windows, dtype=np.float32)
        gammas = np.ascontiguousarray(gammas, dtype=np.float32)
        colors = np.ascontiguousarray(colors, dtype=np.float32)
        visible = np.ascontiguousarray(visible, dtype=np.uint8)
        channels, height, width = raw.shape
        output = np.empty((height, width, 3), dtype=np.uint8)
        error = ctypes.create_string_buffer(1024)
        result = function(
            raw.ctypes.data,
            channels,
            width,
            height,
            windows.ctypes.data,
            gammas.ctypes.data,
            colors.ctypes.data,
            visible.ctypes.data,
            output.ctypes.data,
            error,
            len(error),
        )
        if result == 2:
            pytest.skip(
                "Metal device unavailable in this process; actual-hardware validation must be run separately"
            )
        if result != 0:
            pytest.fail(
                f"native production shader offscreen validation failed: {result}: {error.value!r}"
            )
        return output

    return invoke


def _run(timing_kind="metal_drawable_presented"):
    return {
        "schema": benchmark.RUN_SCHEMA,
        "context": {
            "machine_id": "test-only-machine",
            "source_fingerprint": {"sha256": "a" * 64},
            "cache_fingerprint": {"sha256": "b" * 64},
            "viewport_pixels": [1600, 1200],
            "viewport_points": [800, 600],
            "backing_scale": 2.0,
            "actions_sha256": "c" * 64,
        },
        "timing_kind": timing_kind,
        "samples": [
            {
                "action_id": "lut-1",
                "phase": "resident",
                "dropped": False,
                "input_to_display_ms": 10.0,
                "gpu_ms": 1.0,
                "logical_counters": dict.fromkeys(benchmark.RESIDENT_COUNTERS, 0),
            },
            {
                "action_id": "pan-1",
                "phase": "fresh_tile",
                "dropped": False,
                "input_to_display_ms": 100.0,
                "gpu_ms": 2.0,
            },
        ],
    }


def test_uint16_display_highlights_require_more_than_half_precision():
    raw = np.arange(65472, 65536, dtype=np.uint16).reshape(1, 8, 8)
    displayed = composite(raw, [0], [(65472, 65535)], [(255, 255, 255)], False)
    expected = np.floor(np.arange(64, dtype=np.float64) / 63 * 255).astype(np.uint8)
    np.testing.assert_array_equal(displayed[..., 0].ravel(), expected)
    assert np.unique(displayed[..., 0]).size == 64
    assert displayed[-1, -1, 0] == 255


def test_display_quantizes_by_truncation_not_round_to_nearest():
    raw = np.array([[[0, 1, 2, 3, 4, 5]]], dtype=np.uint16)
    displayed = composite(raw, [0], [(0, 10)], [(255, 255, 255)], False)
    np.testing.assert_array_equal(displayed[0, :, 0], [0, 25, 51, 76, 102, 127])


def test_gamma_brightens_then_adds_and_clamps_without_channel_averaging():
    raw = np.array([[[64, 255]], [[64, 255]]], dtype=np.uint16)
    displayed = composite(
        raw, [0, 1], [(0, 255)] * 2, [(255, 0, 255), (0, 255, 255)], False, [2, 1]
    )
    np.testing.assert_array_equal(displayed, [[[127, 64, 191], [255, 255, 255]]])


def test_all_hidden_channels_produce_black_and_keep_shape():
    raw = np.full((2, 7, 9), 65535, dtype=np.uint16)
    displayed = composite(raw, [], [(0, 65535)] * 2, [(255, 0, 0), (0, 255, 0)], False)
    assert displayed.shape == (7, 9, 3)
    assert not displayed.any()


def test_interpolation_of_raw_before_gamma_does_not_match_browser_display_order():
    endpoints = np.array([[[0, 255]]], dtype=np.uint16)
    displayed = composite(endpoints, [0], [(0, 255)], [(255, 255, 255)], False, [2])
    interpolated_display = (displayed[0, 0, 0].astype(float) + displayed[0, 1, 0]) / 2
    interpolated_raw = composite(
        np.array([[[127.5]]], dtype=np.float32), [0], [(0, 255)], [(255, 255, 255)], False, [2]
    )
    assert interpolated_display == 127.5
    assert interpolated_raw[0, 0, 0] == 180


@pytest.mark.parametrize("shape", [(1, 1, 1), (1, 7, 9), (2, 33, 17), (3, 3, 513), (8, 13, 9)])
@pytest.mark.parametrize("gamma", [0.1, 1.0, 2.2, 10.0])
def test_production_gpu_shader_matches_pre_jpeg_cpu(gpu_composite, shape, gamma):
    raw = np.random.default_rng(10632).integers(0, 65536, shape, dtype=np.uint16)
    channels = shape[0]
    windows = [(700.0 + c * 33, 62000.0 - c * 101) for c in range(channels)]
    colors = [
        ((c * 31 + 61) % 256, (c * 79 + 211) % 256, (c * 53 + 127) % 256) for c in range(channels)
    ]
    visible = [c % 3 != 2 for c in range(channels)]
    gammas = [gamma] * channels
    expected = composite(
        raw, [c for c, on in enumerate(visible) if on], windows, colors, False, gammas
    )
    actual = gpu_composite(raw, windows, gammas, colors, visible)
    difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
    # pow implementations and float32 multiply order can cross one truncation
    # boundary. No larger error is accepted, even for near-half-overflow data.
    assert difference.max(initial=0) <= 1


def test_production_gpu_keeps_all_high_uint16_values_distinct(gpu_composite):
    raw = np.arange(65472, 65536, dtype=np.uint16).reshape(1, 8, 8)
    windows, colors, gammas = [(65472, 65535)], [(255, 255, 255)], [1]
    expected = composite(raw, [0], windows, colors, False, gammas)
    actual = gpu_composite(raw, windows, gammas, colors, [True])
    np.testing.assert_array_equal(actual, expected)


def test_production_gpu_truncates_8bit_and_clamps_additive_sum(gpu_composite):
    raw = np.array([[[0, 1, 2, 3, 4, 5, 10]], [[0, 0, 0, 0, 0, 0, 10]]], dtype=np.uint16)
    windows, colors = [(0, 10)] * 2, [(255, 255, 255)] * 2
    actual = gpu_composite(raw, windows, [1, 1], colors, [True, True])
    np.testing.assert_array_equal(actual[0, :, 0], [0, 25, 51, 76, 102, 127, 255])


def test_production_gpu_hidden_channels_and_partial_edge(gpu_composite):
    raw = np.full((3, 13, 1), 65535, dtype=np.uint16)
    actual = gpu_composite(raw, [(0, 65535)] * 3, [1] * 3, [(255, 255, 255)] * 3, [False] * 3)
    assert actual.shape == (13, 1, 3)
    assert not actual.any()


def test_report_keeps_resident_and_new_tile_latencies_separate():
    summary = benchmark.summarize_run(_run())
    assert summary["phases"]["resident"]["timings_ms"]["input_to_display_ms"]["p50"] == 10
    assert summary["phases"]["fresh_tile"]["timings_ms"]["input_to_display_ms"]["p50"] == 100
    assert summary["resident_no_cpu_pipeline"]["jpeg_encodes"]["all_zero"] is True


def test_percentiles_match_linear_interpolation():
    expected = np.percentile(np.arange(1, 101), [50, 95, 99])
    actual = benchmark.percentiles(list(range(1, 101)))
    np.testing.assert_allclose([actual["p50"], actual["p95"], actual["p99"]], expected)


def test_missing_metrics_are_not_zero_and_incomplete_counters_are_not_proof():
    run = _run()
    del run["samples"][0]["logical_counters"]["source_reads"]
    summary = benchmark.summarize_run(run)
    assert summary["resources"]["peak_rss_bytes"] is None
    assert summary["hardware"]["memory_bandwidth"] is None
    assert summary["hardware"]["power"] is None
    assert summary["phases"]["resident"]["timings_ms"]["upload_ms"]["p50"] is None
    assert summary["resident_no_cpu_pipeline"]["source_reads"]["all_zero"] is None


def test_browser_raf_and_metal_presentation_are_not_reported_as_speedup():
    result = benchmark.compare_runs(_run(), _run("browser_raf_proxy"))
    assert result["presentation_endpoints_match"] is False
    assert result["presentation_speedup"] is None
    assert "different endpoints" in result["comparison_warning"]


@pytest.mark.parametrize("key", benchmark.CONTEXT_FIELDS)
def test_comparison_refuses_every_kind_of_context_mismatch(key):
    first = _run()
    second = copy.deepcopy(first)
    if key == "viewport_points":
        second["context"][key] = [801, 600]
        second["context"]["viewport_pixels"] = [1602, 1200]
    elif key == "viewport_pixels":
        second["context"][key] = [1602, 1200]
        second["context"]["viewport_points"] = [801, 600]
    elif key == "backing_scale":
        second["context"][key] = 1
        second["context"]["viewport_pixels"] = [800, 600]
    elif key == "actions_sha256":
        second["context"][key] = "d" * 64
    elif key.endswith("fingerprint"):
        second["context"][key] = {"sha256": "e" * 64}
    else:
        second["context"][key] = "another-machine"
    with pytest.raises(ValueError, match="contexts do not match"):
        benchmark.compare_runs(first, second)


def test_comparison_rejects_action_reordering_even_with_same_declared_hash():
    first, second = _run(), _run()
    second["samples"].reverse()
    with pytest.raises(ValueError, match="ordered action"):
        benchmark.compare_runs(first, second)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "10"])
def test_invalid_timing_never_enters_percentiles(value):
    run = _run()
    run["samples"][0]["input_to_display_ms"] = value
    with pytest.raises(ValueError, match="finite nonnegative"):
        benchmark.summarize_run(run)


def test_dropped_frames_are_reported_not_zero_latency_samples():
    run = _run()
    sample = run["samples"][0]
    sample["dropped"] = True
    sample["input_to_display_ms"] = None
    report = benchmark.summarize_run(run)
    assert report["phases"]["resident"]["dropped"] == 1
    assert report["phases"]["resident"]["timings_ms"]["input_to_display_ms"]["count"] == 0
    sample["input_to_display_ms"] = 0
    with pytest.raises(ValueError, match="dropped frame"):
        benchmark.summarize_run(run)


def test_hardware_measurements_require_provenance_not_a_logical_byte_count():
    run = _run()
    run["hardware"] = {"memory_bandwidth": 4096}
    with pytest.raises(ValueError, match="value, unit, method, scope"):
        benchmark.summarize_run(run)
    run["hardware"] = {
        "memory_bandwidth": {
            "value": 4096,
            "unit": "bytes/second",
            "method": "test fixture, not a real measurement",
            "scope": "single isolated test process",
        }
    }
    assert benchmark.summarize_run(run)["hardware"]["memory_bandwidth"]["value"] == 4096


def test_action_digest_is_stable_but_order_and_settings_matter():
    actions = [{"action_id": "1", "gamma": 1}, {"action_id": "2", "gamma": 2}]
    assert benchmark.action_digest(actions) == benchmark.action_digest(
        [dict(reversed(list(a.items()))) for a in actions]
    )
    assert benchmark.action_digest(actions) != benchmark.action_digest(list(reversed(actions)))
    actions2 = copy.deepcopy(actions)
    actions2[1]["gamma"] = 3
    assert benchmark.action_digest(actions) != benchmark.action_digest(actions2)


def _native_report():
    context = _run()["context"]
    return {
        "schema": "nd2wsi-native-viewport-v1",
        "viewport": {
            "points": context["viewport_points"],
            "drawable_pixels": context["viewport_pixels"],
        },
        "actions": [
            {"action_id": action["action_id"], "request_count": 10, "uploaded_bytes": 1024}
            for action in benchmark.replay_actions()
        ],
        "frames": [
            {
                "action_id": action["action_id"],
                "mode": "benchmark",
                "complete_viewport": True,
                "presented_time": 100 + index,
                "input_to_present_ms": 12,
                "gpu_ms": 1,
                "class": "resident" if action["kind"] in ("gamma", "visible") else "streamed",
                "request_count": 10,
                "uploaded_bytes": 1024,
                "cpu_readbacks": 0,
            }
            for index, action in enumerate(benchmark.replay_actions())
        ],
        "transfers": {"cpu_rgb_or_jpeg_frames": 0},
        "errors": [],
    }


def test_normalizer_selects_first_complete_actual_presentation_per_action():
    raw = _native_report()
    first = raw["frames"][1]
    raw["frames"] += [dict(first, presented_time=1000, input_to_present_ms=900)]
    raw["frames"].insert(
        0, dict(first, complete_viewport=False, presented_time=1, input_to_present_ms=1)
    )
    result = benchmark.normalize_native_report(raw, _run()["context"])
    assert len(result["samples"]) == 44
    sample = result["samples"][1]
    assert sample["input_to_display_ms"] == 12
    assert sample["logical_counters"]["source_reads"] == 0
    assert sample["logical_counters"]["input_copy_bytes"] == 0
    summary = benchmark.summarize_run(result)
    assert (
        summary["actual_native_raw_tile_residency_timings_ms"]["resident"]["gpu_ms"]["count"] == 23
    )
    assert (
        summary["actual_native_raw_tile_residency_timings_ms"]["streamed"]["gpu_ms"]["count"] == 21
    )


def test_normalizer_preserves_missing_presentations_and_unknown_counters():
    raw = _native_report()
    raw["frames"] = raw["frames"][1:]
    del raw["frames"][0]["request_count"]
    result = benchmark.normalize_native_report(raw, _run()["context"])
    assert result["samples"][0]["dropped"] is True
    assert result["samples"][0]["input_to_display_ms"] is None
    assert result["samples"][1]["logical_counters"]["source_reads"] is None


@pytest.mark.parametrize("invalid", ["errors", "viewport", "missing_action"])
def test_normalizer_refuses_incomparable_or_failed_native_runs(invalid):
    raw = _native_report()
    if invalid == "errors":
        raw["errors"] = ["request failed"]
    elif invalid == "viewport":
        raw["viewport"]["points"] = [1, 1]
    else:
        raw["actions"].pop()
    with pytest.raises(ValueError):
        benchmark.normalize_native_report(raw, _run()["context"])
