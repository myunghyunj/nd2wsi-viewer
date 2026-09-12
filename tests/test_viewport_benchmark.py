import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from nd2wsi import render

pytest.importorskip("resource", reason="macOS packaged viewport resource diagnostics")
from nd2wsi import viewport_benchmark as benchmark  # noqa: E402


def test_packaged_replay_matches_original_benchmark():
    path = Path(__file__).parents[1] / "scripts" / "benchmark_metal_viewport.py"
    spec = importlib.util.spec_from_file_location("reporter", path)
    reporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reporter)
    assert benchmark.replay_actions() == reporter.replay_actions()
    assert len(benchmark.replay_actions()) == 44


def test_counters_disabled_normally_and_restore(monkeypatch):
    assert benchmark.metrics_snapshot()["ok"] is False
    monkeypatch.setattr(render, "_read_region", lambda *args: "pixels")
    monkeypatch.setattr(render, "composite", lambda *args: "rgb")
    monkeypatch.setattr(render, "encode_image", lambda *args: "jpeg")
    original = render._read_region
    restore = benchmark._install_counters()
    wrapper = render._read_region
    try:
        assert wrapper(None, "0") == "pixels"
        render.composite(None)
        render.encode_image(None, "jpeg")
        snapshot = benchmark.metrics_snapshot()
        assert snapshot["counters"]["source_reads"] == 1
        assert snapshot["counters"]["raw_region_reads"] == 1
        assert snapshot["counters"]["cpu_rgb_tiles"] == 1
        assert snapshot["counters"]["jpeg_encodes"] == 1
        assert snapshot["counters"]["decoded_tiles"] is None
    finally:
        restore()
    assert render._read_region is original
    assert benchmark.metrics_snapshot()["ok"] is False
    # A renderer invocation that was already in flight may finish after restore.
    assert wrapper(None, "1") == "pixels"


def test_non_agent_replay_cannot_touch_source(tmp_path):
    window = SimpleNamespace(destroy=lambda: None)
    session = SimpleNamespace(role="user", as_dict=lambda: {"role": "user"})
    api = SimpleNamespace(_window_session=session)
    report = benchmark.run_browser_replay(api, window, tmp_path / "report.json",
                                          tmp_path / "absent.json", tmp_path)
    assert report["ok"] is False
    assert "Agent" in report["error"]
    assert not (tmp_path / "report.json").exists()


def test_pair_summary_does_not_turn_proxy_into_present(monkeypatch):
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    from benchmark_rc2_paired import aggregate_pairs

    context = {"machine_id": "isolated", "source_fingerprint": {"sha256": "a" * 64},
               "cache_fingerprint": {"sha256": "b" * 64}, "viewport_pixels": [1024, 650],
               "viewport_points": [1024, 650], "backing_scale": 1, "actions_sha256": "c" * 64}

    def run(timing):
        return {"schema": "nd2wsi-viewport-run/1", "context": context,
                "timing_kind": timing, "samples": [{"action_id": "step-1", "phase": "resident",
                "dropped": False, "input_to_display_ms": 10, "observed_native_residency": "resident"}]}

    pair = {"order": ["native", "browser"], "native": run("metal_drawable_presented"),
            "browser": run("browser_raf_proxy")}
    pair["browser"]["resources"] = {"peak_rss_bytes": None,
                                   "memory_note": "Host only, excluding WebKit helpers"}
    with pytest.raises(ValueError, match="five"):
        aggregate_pairs([pair])
    result = aggregate_pairs([pair] * 5)
    assert result["performance_gate"]["state"] == "unverified"
    assert result["performance_gate"]["automatic_metal_default"] is False
    assert result["browser_input_to_present_ms"] is None
    assert result["actual_native_residency"]["streamed"]["pooled_ms"]["p95"] is None
    assert result["fixed_action_workloads"]["resident"]["native"]["actions"] == 5
