"""Verify live LUT updates in the translucent overlay in one private Agent window."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

os.environ["ND2WSI_DISABLE_UPDATES"] = "1"
os.environ["ND2WSI_APP_NAME"] = "nd2wsi-viewer Live LUT QA"

if importlib.util.find_spec("nd2wsi") is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import tifffile  # noqa: E402
import webview  # noqa: E402

import nd2wsi  # noqa: E402
import nd2wsi.server  # noqa: E402
from nd2wsi import app  # noqa: E402
from nd2wsi.translucent_smoke import (  # noqa: E402
    bridge_result,
    camera,
    check_camera,
    check_roi_boxes,
    click_shell_cloud,
    native_state,
    wait_for,
)
from nd2wsi.window_sessions import create_window_session  # noqa: E402

qa = Path(os.environ["ASTROMAP_QA_ROOT"]).resolve()
report_path = qa / os.environ.get("ASTROMAP_QA_REPORT", "live-lut-native-check.json")
session = create_window_session("agent", qa / "sessions")
os.environ["ND2WSI_LUT_SETTINGS_ROOT"] = str(session.exports_root / "lut-settings")
os.environ["ND2WSI_UI_SETTINGS_ROOT"] = str(session.exports_root / "ui-settings")
api, parent = app.create_app_window(None, window_session=session)
report = {
    "ok": False,
    "module": nd2wsi.__file__,
    "session": session.as_dict(),
    "scope": "live-lut-translucent-native",
    "checks": {},
}

CENTER = (256.0, 256.0)
DRAG_TARGETS = (
    ("initial", 255.0, 64.0),
    ("hold_192", 192.0, 85.0),
    ("hold_128", 128.0, 127.0),
    ("release_80", 80.0, 204.0),
)
CAL_TOLERANCE = 8.0


def write_report():
    report["exports_report"] = str(session.exports_root / "live-lut-native-report.json")
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (session.exports_root / "live-lut-native-report.json").write_text(payload, encoding="utf-8")
    report_path.write_text(payload, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


def check(value, message):
    if not value:
        raise AssertionError(message)


def _pane_eval(expression):
    """Run a diagnostic expression in the active slide pane, self-contained for 2.6.9+."""
    body = expression.strip()
    if ";" in body and not body.startswith(("(()", "(function")):
        head, tail = body.rsplit(";", 1)
        tail = tail.strip()
        stmt = f"{head}; return ({tail});" if tail else f"{head}; return true;"
    else:
        stmt = f"return ({body});"
    return parent.evaluate_js(
        "(() => {"
        " const __frame = globalThis.document.querySelector('#frames iframe.active');"
        " const __win = __frame && __frame.contentWindow;"
        " if (!__win) return null;"
        " const state = __win.nd2ViewerState;"
        " const document = __win.document;"
        " const Nd2ViewerGeometry = __win.Nd2ViewerGeometry;"
        " const OpenSeadragon = __win.OpenSeadragon;"
        " const captureTranslucentView = __win.captureTranslucentView;"
        " const refreshTiles = __win.refreshTiles;"
        " const tileQuery = __win.tileQuery;"
        " " + stmt +
        "})()"
    )


def sample_canvas_patch(window, image_x, image_y, *, is_pane=False):
    """Sample a 5x5 image-centered patch from the OSD 2D canvas (pre-opacity)."""
    viewer_expr = "state.viewer" if is_pane else "window.nd2Translucent && window.nd2Translucent.viewer"
    script = f"""(() => {{
      const viewer = {viewer_expr};
      if (!viewer || !viewer.viewport || !viewer.drawer) return {{ error: "viewer not ready" }};
      const vp = viewer.viewport;
      const size = vp.getContainerSize();
      const canvas = viewer.drawer.canvas;
      const ctx = viewer.drawer.context || (canvas && canvas.getContext("2d"));
      if (!canvas || !ctx || !(size.x > 0) || !(size.y > 0) || !(canvas.width > 0) || !(canvas.height > 0)) {{
        return {{ error: "canvas not ready" }};
      }}
      const sx = canvas.width / size.x;
      const sy = canvas.height / size.y;
      const coords = [];
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (let dy = -2; dy <= 2; dy++) {{
        for (let dx = -2; dx <= 2; dx++) {{
          const pt = Nd2ViewerGeometry.imageToViewerElementPoint(
            vp, {{ x: {float(image_x)!r} + dx + 0.5, y: {float(image_y)!r} + dy + 0.5 }}, OpenSeadragon
          );
          const bx = Math.floor(pt.x * sx);
          const by = Math.floor(pt.y * sy);
          if (!Number.isFinite(bx) || !Number.isFinite(by) ||
              bx < 0 || bx >= canvas.width || by < 0 || by >= canvas.height) {{
            return {{
              error: "backing coordinate out of bounds",
              coord: [bx, by],
              canvas: [canvas.width, canvas.height],
            }};
          }}
          coords.push([bx, by]);
          if (bx < minX) minX = bx;
          if (by < minY) minY = by;
          if (bx > maxX) maxX = bx;
          if (by > maxY) maxY = by;
        }}
      }}
      const bw = maxX - minX + 1;
      const bh = maxY - minY + 1;
      const img = ctx.getImageData(minX, minY, bw, bh).data;
      let sumR = 0, sumG = 0, sumB = 0, minAlpha = 255;
      for (const [bx, by] of coords) {{
        const off = ((by - minY) * bw + (bx - minX)) * 4;
        sumR += img[off];
        sumG += img[off + 1];
        sumB += img[off + 2];
        if (img[off + 3] < minAlpha) minAlpha = img[off + 3];
      }}
      const n = coords.length;
      const meanRgb = [sumR / n, sumG / n, sumB / n];
      return {{
        mean_rgb: meanRgb,
        mean_intensity: (meanRgb[0] + meanRgb[1] + meanRgb[2]) / 3,
        min_alpha: minAlpha,
        samples: n,
      }};
    }})()"""
    result = None
    for attempt in range(3):
        result = _pane_eval(script) if is_pane else window.evaluate_js(script)
        if result is not None:
            break
        if attempt < 2:
            time.sleep(0.05)
    if not isinstance(result, dict) or "error" in result:
        raise AssertionError(f"sample_canvas_patch failed ({'pane' if is_pane else 'overlay'}): {result}")
    return result


def build_fixtures():
    """Write 1-channel and 2-channel uint8 calibration OME-TIFF fixtures under exports_root."""
    # Single-channel neutral #FFFFFF (OME signed 32-bit RGBA 0xFFFFFFFF == -1)
    cal1_path = session.exports_root / "cal_1ch.ome.tif"
    arr1 = np.zeros((1, 512, 512), dtype=np.uint8)
    arr1[0, 128:384, 128:384] = 64
    arr1[0, :16, :] = 255
    tifffile.imwrite(
        cal1_path,
        arr1,
        ome=True,
        tile=(256, 256),
        photometric="minisblack",
        metadata={
            "axes": "CYX",
            "Channel": {"Name": ["CalWhite"], "Color": [-1]},
        },
    )

    # Two-channel: CalA (#00FF00 -> 0x00FF00FF = 16711935), CalB (#FF00FF -> 0xFF00FFFF = -16711681).
    # Background 1 with a 0 bottom strip and 255 top strip gives default window [1, 255]
    # and axis [0, 255] so resetting to metadata/defaults keeps the 64 center patch visible (~63).
    cal2_path = session.exports_root / "cal_2ch.ome.tif"
    arr2 = np.ones((2, 512, 512), dtype=np.uint8)
    arr2[:, -16:, :] = 0
    arr2[:, :16, :] = 255
    arr2[:, 128:384, 128:384] = 64
    tifffile.imwrite(
        cal2_path,
        arr2,
        ome=True,
        tile=(256, 256),
        photometric="minisblack",
        metadata={
            "axes": "CYX",
            "Channel": {"Name": ["CalA", "CalB"], "Color": [16711935, -16711681]},
        },
    )
    return cal1_path, cal2_path


def _ensure_channels_open(expected_count):
    _pane_eval("if (state.windows.channels.isHidden()) state.windows.channels.open(); true")
    wait_for(
        lambda: _pane_eval(
            f"(() => {{"
            f" if (!state.info || state.info.channels.length !== {expected_count}) return false;"
            f" if (state.lutAutoPending.some(Boolean)) return false;"
            f" const cs = [...document.querySelectorAll('.lut-canvas')];"
            f" if (cs.length !== {expected_count}) return false;"
            f" return cs.every(c => {{"
            f"   const r = c.getBoundingClientRect();"
            f"   const a = Number(c.getAttribute('data-axis-min'));"
            f"   const b = Number(c.getAttribute('data-axis-max'));"
            f"   return r.width >= 150 && r.height >= 60 && Number.isFinite(a) && Number.isFinite(b) && b > a;"
            f" }});"
            f"}})()"
        ),
        timeout=20,
        description=f"channels panel with {expected_count} LUT canvas(es)",
    )


def _reset_channel_luts(lo=0, hi=255, gamma=1):
    return _pane_eval(
        f"(() => {{"
        f" const results = [];"
        f" for (let i = 0; i < state.lutWidgets.length; i++) {{"
        f"   results.push(state.lutWidgets[i].setLut({{ lo: {lo}, hi: {hi}, gamma: {gamma} }}));"
        f" }}"
        f" return {{ results, luts: state.luts, query: typeof tileQuery === 'function' ? tileQuery() : null }};"
        f"}})()"
    )


def _wait_for_intensity(window, target, *, is_pane, timeout=10, description="intensity"):
    last_diag = None

    def probe():
        nonlocal last_diag
        sample = sample_canvas_patch(window, *CENTER, is_pane=is_pane)
        if _within(sample, target):
            return sample
        if is_pane:
            last_diag = {
                "sample": sample,
                "luts": _pane_eval("state.luts"),
                "query": _pane_eval("typeof tileQuery === 'function' ? tileQuery() : null"),
                "fullyLoaded": _pane_eval("state.viewer.world.getItemAt(0)?.getFullyLoaded()"),
            }
        else:
            last_diag = {
                "sample": sample,
                "query": _overlay_query_dict(window),
                "fullyLoaded": window.evaluate_js("nd2Translucent.viewer.world.getItemAt(0)?.getFullyLoaded()"),
            }
        return None

    try:
        return wait_for(probe, timeout=timeout, description=description)
    except TimeoutError as exc:
        raise TimeoutError(f"{exc} (last={last_diag})") from exc


def _main_query_dict():
    raw = _pane_eval("typeof tileQuery === 'function' ? tileQuery() : ''") or ""
    return _parse_query(raw)


def _overlay_query_dict(child):
    raw = child.evaluate_js(
        "(() => {"
        " const item = window.nd2Translucent?.viewer?.world?.getItemAt?.(0);"
        " const url = item?.source?.getTileUrl?.(0, 0, 0) || '';"
        " const qIdx = url.indexOf('?');"
        " const fromUrl = qIdx >= 0 ? url.slice(qIdx + 1) : '';"
        " const stateQuery = typeof window.nd2Translucent?.state?.query === 'string' ? window.nd2Translucent.state.query : null;"
        " return { url, fromUrl, stateQuery };"
        "})()"
    )
    parsed = _parse_query(raw["fromUrl"])
    return {"raw": raw["fromUrl"], "state_query": raw["stateQuery"], "params": parsed}


def _parse_query(raw):
    text = str(raw or "")
    if "?" in text:
        text = urlsplit(text).query if "://" in text else text.split("?", 1)[1]
    return {k: v[0] if len(v) == 1 else v for k, v in parse_qs(text, keep_blank_values=True).items()}


def _dispatch_hi_pointer(ch_index, event_type, target_value=None, pointer_id=77):
    """Dispatch PointerEvent on .lut-canvas white-point handle; resilient to 2.6.9 setPointerCapture."""
    val_js = "null" if target_value is None else repr(float(target_value))
    return _pane_eval(
        f"(() => {{"
        f" const canvas = document.querySelectorAll('.lut-canvas')[{ch_index}];"
        f" if (!canvas) return {{ error: 'missing lut canvas' }};"
        f" const r = canvas.getBoundingClientRect();"
        f" const vmin = Number(canvas.getAttribute('data-axis-min'));"
        f" const vmax = Number(canvas.getAttribute('data-axis-max'));"
        f" const ch = state.info.channels[{ch_index}];"
        f" const cur = state.luts[{ch_index}] || {{ lo: ch.window.start, hi: ch.window.end, gamma: 1 }};"
        f" const val = ({val_js}) === null ? cur.hi : ({val_js});"
        f" const x0 = 4, x1 = r.width - 4;"
        f" const frac = Math.max(0, Math.min(1, (val - vmin) / (vmax - vmin)));"
        f" const cx = r.left + x0 + frac * (x1 - x0);"
        f" const cy = r.top + 8;"
        f" const ev = new PointerEvent({json.dumps(event_type)}, {{"
        f"   pointerId: {pointer_id}, pointerType: 'mouse', isPrimary: true,"
        f"   button: 0, buttons: {0 if event_type in ('pointerup', 'pointercancel') else 1},"
        f"   clientX: cx, clientY: cy, bubbles: true, cancelable: true"
        f" }});"
        f" canvas.dispatchEvent(ev);"
        f" return {{ val, cx, cy, vmin, vmax, luts: state.luts[{ch_index}] }};"
        f"}})()"
    )


def _dispatch_gamma_drag(ch_index, target_gamma, pointer_id=79):
    """Drag channel gamma knob vertically to target_gamma and release."""
    return _pane_eval(
        f"(() => {{"
        f" const canvas = document.querySelectorAll('.lut-canvas')[{ch_index}];"
        f" if (!canvas) return {{ error: 'missing lut canvas' }};"
        f" const r = canvas.getBoundingClientRect();"
        f" const vmin = Number(canvas.getAttribute('data-axis-min'));"
        f" const vmax = Number(canvas.getAttribute('data-axis-max'));"
        f" const ch = state.info.channels[{ch_index}];"
        f" const cur = state.luts[{ch_index}] || {{ lo: ch.window.start, hi: ch.window.end, gamma: 1 }};"
        f" const x0 = 4, x1 = r.width - 4, y0 = 14, y1 = r.height - 18;"
        f" const mid = (cur.lo + cur.hi) / 2;"
        f" const kx = r.left + x0 + Math.max(0, Math.min(1, (mid - vmin) / (vmax - vmin))) * (x1 - x0);"
        f" const ky0 = r.top + (y1 - Math.pow(0.5, 1 / cur.gamma) * (y1 - y0));"
        f" const ky1 = r.top + (y1 - Math.pow(0.5, 1 / {float(target_gamma)!r}) * (y1 - y0));"
        f" const send = (type, x, y, buttons) => canvas.dispatchEvent(new PointerEvent(type, {{"
        f"   pointerId: {pointer_id}, pointerType: 'mouse', isPrimary: true,"
        f"   button: 0, buttons, clientX: x, clientY: y, bubbles: true, cancelable: true"
        f" }}));"
        f" send('pointerdown', kx, ky0, 1);"
        f" send('pointermove', kx, ky1, 1);"
        f" send('pointerup', kx, ky1, 0);"
        f" return {{ luts: state.luts[{ch_index}], kx, ky0, ky1 }};"
        f"}})()"
    )


def _within(sample, target, tol=CAL_TOLERANCE):
    return sample["min_alpha"] == 255 and abs(sample["mean_intensity"] - target) <= tol


def _poll_both_for_target(child, target, *, hold_seconds, tol=CAL_TOLERANCE):
    """Poll main and overlay canvases up to hold_seconds, recording first hit & elapsed ms."""
    t0 = time.monotonic()
    deadline = t0 + hold_seconds
    main_hit_ms = None
    overlay_hit_ms = None
    main_hit_sample = None
    overlay_hit_sample = None
    main_last = None
    overlay_last = None
    while True:
        now_ms = round((time.monotonic() - t0) * 1000.0, 1)
        main_last = sample_canvas_patch(parent, *CENTER, is_pane=True)
        overlay_last = sample_canvas_patch(child, *CENTER, is_pane=False)
        if main_hit_ms is None and _within(main_last, target, tol):
            main_hit_ms = now_ms
            main_hit_sample = main_last
        if overlay_hit_ms is None and _within(overlay_last, target, tol):
            overlay_hit_ms = now_ms
            overlay_hit_sample = overlay_last
        if main_hit_ms is not None and overlay_hit_ms is not None:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return {
        "target": target,
        "main_hit": main_hit_ms is not None,
        "overlay_hit": overlay_hit_ms is not None,
        "main_elapsed_ms": main_hit_ms,
        "overlay_elapsed_ms": overlay_hit_ms,
        "main": main_hit_sample or main_last,
        "overlay": overlay_hit_sample or overlay_last,
    }


def _run_continuous_drag_sequence(child, *, hold_seconds=1.0, release_seconds=2.5):
    """Drive hi: 255 -> 192 -> 128 -> release 80 and collect pre-release and post-release evidence."""
    steps = {}
    main_init = sample_canvas_patch(parent, *CENTER, is_pane=True)
    overlay_init = sample_canvas_patch(child, *CENTER, is_pane=False)
    steps["initial"] = {
        "target": 64.0,
        "main_hit": _within(main_init, 64.0),
        "overlay_hit": _within(overlay_init, 64.0),
        "main": main_init,
        "overlay": overlay_init,
    }

    _dispatch_hi_pointer(0, "pointerdown", target_value=255.0)
    try:
        # Hold 1: hi -> 192 (expect ~85) BEFORE pointerup
        _dispatch_hi_pointer(0, "pointermove", target_value=192.0)
        steps["hold_192"] = _poll_both_for_target(child, 85.0, hold_seconds=hold_seconds)

        # Hold 2: hi -> 128 (expect ~127) BEFORE pointerup
        _dispatch_hi_pointer(0, "pointermove", target_value=128.0)
        steps["hold_128"] = _poll_both_for_target(child, 127.0, hold_seconds=hold_seconds)

        # Release: hi -> 80 (expect ~204)
        _dispatch_hi_pointer(0, "pointermove", target_value=80.0)
    finally:
        _dispatch_hi_pointer(0, "pointerup", target_value=80.0)

    steps["release_80"] = _poll_both_for_target(child, 204.0, hold_seconds=release_seconds)

    main_ok = all(steps[k]["main_hit"] for k, _, _ in DRAG_TARGETS)
    overlay_ok = all(steps[k]["overlay_hit"] for k, _, _ in DRAG_TARGETS)
    main_series = [round(steps[k]["main"]["mean_intensity"], 2) for k, _, _ in DRAG_TARGETS]
    overlay_series = [round(steps[k]["overlay"]["mean_intensity"], 2) for k, _, _ in DRAG_TARGETS]
    return {
        "ok": main_ok and overlay_ok,
        "main_ok": main_ok,
        "overlay_ok": overlay_ok,
        "main_intensities": main_series,
        "overlay_intensities": overlay_series,
        "steps": steps,
    }


def _distinct_intermediate_levels(samples, *, baseline=64.0, min_gap=8.0):
    """Count distinct opaque patch intensities above baseline separated by >= min_gap."""
    levels = []
    for sample in samples:
        if sample.get("min_alpha") != 255:
            continue
        val = float(sample["mean_intensity"])
        if val < baseline + min_gap:
            continue
        if all(abs(val - existing) >= min_gap for existing in levels):
            levels.append(round(val, 2))
    return levels


def check_1_continuous_drag(child):
    """Check 1: continuous drag hi 255->192->128->80 with pre-release holds and separate opacity check."""
    _reset_channel_luts(0, 255, 1)
    _wait_for_intensity(parent, 64.0, is_pane=True, timeout=10, description="main baseline intensity 64")
    _wait_for_intensity(child, 64.0, is_pane=False, timeout=10, description="overlay baseline intensity 64")
    opacity_before = bridge_result(child, "pywebview.api.translucent_context()")["opacity"]
    stage_opacity_before = child.evaluate_js(
        "Number(getComputedStyle(document.getElementById('translucent-stage')).opacity)"
    )

    drag_result = _run_continuous_drag_sequence(child, hold_seconds=1.0, release_seconds=2.5)

    opacity_after = bridge_result(child, "pywebview.api.translucent_context()")["opacity"]
    stage_opacity_after = child.evaluate_js(
        "Number(getComputedStyle(document.getElementById('translucent-stage')).opacity)"
    )
    opacity_ok = (
        math.isclose(opacity_before, 0.55, abs_tol=1e-4)
        and math.isclose(opacity_after, opacity_before, abs_tol=1e-4)
        and math.isclose(stage_opacity_before, stage_opacity_after, abs_tol=1e-4)
    )
    errors = []
    if not drag_result["main_ok"]:
        errors.append(
            f"Main viewer did not hit [64, 85, 127, 204] ± 8 before/after release: {drag_result['main_intensities']}"
        )
    if not drag_result["overlay_ok"]:
        errors.append(
            f"Translucent overlay did not hit [64, 85, 127, 204] ± 8 before/after release: {drag_result['overlay_intensities']}"
        )
    if not opacity_ok:
        errors.append(
            f"Overlay opacity changed unexpectedly: context {opacity_before}->{opacity_after}, "
            f"stage {stage_opacity_before}->{stage_opacity_after}"
        )
    return {
        **drag_result,
        "opacity": {
            "context_before": opacity_before,
            "context_after": opacity_after,
            "stage_before": stage_opacity_before,
            "stage_after": stage_opacity_after,
            "ok": opacity_ok,
        },
        "ok": drag_result["ok"] and opacity_ok,
        "error": "; ".join(errors) if errors else None,
    }


def check_2_slow_tiles(child):
    """Check 2: continuous ~16ms pointermove for >=2s under 250ms tile render delay."""
    _reset_channel_luts(0, 255, 1)
    _wait_for_intensity(parent, 64.0, is_pane=True, timeout=10, description="main reset to 64 before slow-tile check")
    # Wait briefly for overlay reset (on 2.6.9 it is already at 64; on 2.6.10 it settles back to 64).
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if _within(sample_canvas_patch(child, *CENTER, is_pane=False), 64.0):
            break
        time.sleep(0.05)

    orig_render = nd2wsi.server._render_tile_compat
    slow_calls = {"count": 0}

    def slow_render(*args, **kwargs):
        slow_calls["count"] += 1
        time.sleep(0.25)
        return orig_render(*args, **kwargs)

    nd2wsi.server._render_tile_compat = slow_render
    try:
        # Install overlay rAF sampler to measure painted frames, backdrop coverage, and banners.
        child.evaluate_js("""(() => {
          const vp = window.nd2Translucent.viewer.viewport;
          const drawer = window.nd2Translucent.viewer.drawer;
          const backdrop = document.getElementById('translucent-backdrop');
          const status = document.getElementById('overlay-status');
          const rec = { running: true, samples: [], banners: [], unbackedTransparent: 0 };
          window.__qaSlowOverlay = rec;
          const sampleCanvas = (canvas) => {
            if (!canvas || !(canvas.width > 0) || !(canvas.height > 0)) return null;
            const ctx = canvas.getContext('2d');
            const size = vp.getContainerSize();
            if (!ctx || !(size.x > 0) || !(size.y > 0)) return null;
            const sx = canvas.width / size.x, sy = canvas.height / size.y;
            const pt = Nd2ViewerGeometry.imageToViewerElementPoint(vp, { x: 256.5, y: 256.5 }, OpenSeadragon);
            const bx = Math.floor(pt.x * sx), by = Math.floor(pt.y * sy);
            if (bx < 2 || by < 2 || bx >= canvas.width - 2 || by >= canvas.height - 2) return null;
            const data = ctx.getImageData(bx - 2, by - 2, 5, 5).data;
            let sr = 0, sg = 0, sb = 0, minA = 255;
            for (let i = 0; i < data.length; i += 4) {
              sr += data[i]; sg += data[i + 1]; sb += data[i + 2];
              if (data[i + 3] < minA) minA = data[i + 3];
            }
            const n = data.length / 4;
            return { mean_intensity: (sr + sg + sb) / (3 * n), min_alpha: minA };
          };
          const tick = () => {
            if (!rec.running) return;
            const s = sampleCanvas(drawer.canvas);
            if (s) {
              rec.samples.push({ t: Math.round(performance.now()), ...s });
              if (s.min_alpha < 255) {
                const bg = backdrop && !backdrop.hidden ? sampleCanvas(backdrop) : null;
                if (!bg || bg.min_alpha < 255) rec.unbackedTransparent++;
              }
            }
            if (status && !status.hidden && /could not load|reopen/i.test(status.textContent || '')) {
              rec.banners.push(status.textContent);
            }
            requestAnimationFrame(tick);
          };
          requestAnimationFrame(tick);
          return true;
        })()""")

        # Drive continuous ~16ms pointermove in the slide pane for >= 2.2s WITHOUT releasing yet.
        _pane_eval("""(() => {
          const canvas = document.querySelectorAll('.lut-canvas')[0];
          const r = canvas.getBoundingClientRect();
          const vmin = Number(canvas.getAttribute('data-axis-min'));
          const vmax = Number(canvas.getAttribute('data-axis-max'));
          const x0 = 4, x1 = r.width - 4;
          const cx = v => r.left + x0 + Math.max(0, Math.min(1, (v - vmin) / (vmax - vmin))) * (x1 - x0);
          const cy = r.top + 8;
          const vp = state.viewer.viewport;
          const drawer = state.viewer.drawer;
          const rec = {
            done: false, steps: 0, startMs: performance.now(), elapsedMs: 0,
            samples: [], banners: [],
          };
          __win.__qaSlowMain = rec;
          const sampleMain = () => {
            const c = drawer.canvas;
            const ctx = drawer.context || (c && c.getContext('2d'));
            const size = vp.getContainerSize();
            if (!c || !ctx || !(size.x > 0) || !(size.y > 0)) return null;
            const sx = c.width / size.x, sy = c.height / size.y;
            const pt = Nd2ViewerGeometry.imageToViewerElementPoint(vp, { x: 256.5, y: 256.5 }, OpenSeadragon);
            const bx = Math.floor(pt.x * sx), by = Math.floor(pt.y * sy);
            if (bx < 2 || by < 2 || bx >= c.width - 2 || by >= c.height - 2) return null;
            const data = ctx.getImageData(bx - 2, by - 2, 5, 5).data;
            let sr = 0, sg = 0, sb = 0, minA = 255;
            for (let i = 0; i < data.length; i += 4) {
              sr += data[i]; sg += data[i + 1]; sb += data[i + 2];
              if (data[i + 3] < minA) minA = data[i + 3];
            }
            const n = data.length / 4;
            return { mean_intensity: (sr + sg + sb) / (3 * n), min_alpha: minA };
          };
          const dispatch = (type, val, buttons) => canvas.dispatchEvent(new PointerEvent(type, {
            pointerId: 88, pointerType: 'mouse', isPrimary: true,
            button: 0, buttons, clientX: cx(val), clientY: cy, bubbles: true, cancelable: true,
          }));
          dispatch('pointerdown', 255, 1);
          const durationMs = 2200;
          const timer = setInterval(() => {
            const elapsed = performance.now() - rec.startMs;
            const frac = Math.min(1, elapsed / durationMs);
            const hi = 250 - frac * (250 - 85);
            dispatch('pointermove', hi, 1);
            rec.steps++;
            rec.elapsedMs = Math.round(elapsed);
            if (elapsed >= durationMs) {
              clearInterval(timer);
              rec.done = true;
            }
          }, 16);
          const raf = () => {
            const s = sampleMain();
            if (s) rec.samples.push({ t: Math.round(performance.now() - rec.startMs), ...s });
            const toast = document.getElementById('toast');
            if (toast && toast.classList.contains('show') && /failed to load/i.test(toast.textContent || '')) {
              rec.banners.push(toast.textContent);
            }
            if (!rec.done) requestAnimationFrame(raf);
          };
          requestAnimationFrame(raf);
          return true;
        })()""")

        wait_for(lambda: _pane_eval("Boolean(__win.__qaSlowMain && __win.__qaSlowMain.done)"),
                 timeout=10, description="slow-tile continuous drag phase")

        # Snapshot all pre-release samples BEFORE dispatching pointerup!
        main_pre = _pane_eval("__win.__qaSlowMain")
        overlay_pre = child.evaluate_js(
            "(() => { const r = window.__qaSlowOverlay; r.running = false; return r; })()"
        )

        # Now release at hi = 76 (expect int(64/76*255) = 214; fresh value never cached in Check 1).
        _dispatch_hi_pointer(0, "pointermove", target_value=76.0, pointer_id=88)
        _dispatch_hi_pointer(0, "pointerup", target_value=76.0, pointer_id=88)

        settled = _poll_both_for_target(child, 214.0, hold_seconds=6.0)
        # Allow any straggling delayed responses to land and verify no false reopen banner appears.
        time.sleep(0.4)
        overlay_banner_after = child.evaluate_js(
            "(() => { const s = document.getElementById('overlay-status');"
            " return s && !s.hidden ? (s.textContent || '') : ''; })()"
        )
        main_toast_after = _pane_eval(
            "(() => { const t = document.getElementById('toast');"
            " return t && t.classList.contains('show') ? (t.textContent || '') : ''; })()"
        )
    finally:
        nd2wsi.server._render_tile_compat = orig_render
        _pane_eval("delete __win.__qaSlowMain; true")
        child.evaluate_js("if (window.__qaSlowOverlay) window.__qaSlowOverlay.running = false; delete window.__qaSlowOverlay; true")

    main_levels = _distinct_intermediate_levels(main_pre["samples"], baseline=64.0, min_gap=8.0)
    overlay_levels = _distinct_intermediate_levels(overlay_pre["samples"], baseline=64.0, min_gap=8.0)
    overlay_banners = list(overlay_pre["banners"])
    if overlay_banner_after and ("could not load" in overlay_banner_after.lower() or "reopen" in overlay_banner_after.lower()):
        overlay_banners.append(overlay_banner_after)
    main_banners = list(main_pre["banners"])
    if main_toast_after and "failed to load" in main_toast_after.lower():
        main_banners.append(main_toast_after)

    errors = []
    if main_pre["elapsedMs"] < 2000 or main_pre["steps"] < 60:
        errors.append(f"Slow drag duration/steps insufficient: {main_pre['elapsedMs']}ms, {main_pre['steps']} steps")
    if slow_calls["count"] < 2:
        errors.append(f"Slow tile hook did not intercept enough tile renders: {slow_calls['count']}")
    if len(main_levels) < 2:
        errors.append(f"Main viewer starved under 250ms tiles (<2 distinct intermediate levels): {main_levels}")
    if len(overlay_levels) < 2:
        errors.append(f"Overlay starved under 250ms tiles (<2 distinct intermediate levels): {overlay_levels}")
    if not settled["main_hit"]:
        errors.append(f"Main viewer did not settle to 214 ± 8 after release: {settled['main']}")
    if not settled["overlay_hit"]:
        errors.append(f"Overlay did not settle to 214 ± 8 after release: {settled['overlay']}")
    if overlay_banners or main_banners:
        errors.append(f"False tile-load-failed banner(s): main={main_banners}, overlay={overlay_banners}")

    return {
        "ok": not errors,
        "drag_steps": main_pre["steps"],
        "drag_elapsed_ms": main_pre["elapsedMs"],
        "slow_render_calls": slow_calls["count"],
        "main_intermediate_levels": main_levels,
        "overlay_intermediate_levels": overlay_levels,
        "overlay_unbacked_transparent_frames": overlay_pre["unbackedTransparent"],
        "settled": settled,
        "main_banners": main_banners,
        "overlay_banners": overlay_banners,
        "error": "; ".join(errors) if errors else None,
    }


def check_3_tracing_alignment(child):
    """Check 3: independent move/zoom + diagnostic ROI; verify live LUT while locked AND unlocked."""
    # Independently move native window and zoom/pan overlay (keeping center (256, 256) visible).
    before_move = native_state(child)["frame"]
    check(bridge_result(child, "pywebview.api.move_window(24, 18)"), "Native overlay move failed")
    wait_for(lambda: native_state(child)["frame"][:2] != before_move[:2], timeout=10, description="overlay move")
    child.evaluate_js("""(() => {
      const v = nd2Translucent.viewer, p = v.viewport;
      p.zoomTo(p.imageToViewportZoom(1.2), null, true);
      p.panTo(p.imageToViewportCoordinates(272, 268), true);
      v.forceRedraw();
      return true;
    })()""")
    wait_for(lambda: child.evaluate_js("nd2Translucent.viewer.world.getItemAt(0).getFullyLoaded()"),
             timeout=15, description="aligned overlay tiles")

    rois = [
        {"x": 160.0, "y": 160.0, "w": 64.0, "h": 64.0, "index": 1, "label": "Cal ROI 1"},
        {"x": 280.0, "y": 240.0, "w": 64.0, "h": 64.0, "index": 2, "label": "Cal ROI 2"},
    ]
    payload = {"ok": True, "name": "live-lut diagnostic ROI", "rois": rois}
    check(child.evaluate_js("nd2Translucent.setRois(" + json.dumps(payload) + ", {pan: false, index: 0})"),
          "Diagnostic ROI boxes were not accepted")
    check_roi_boxes(child, expected_rois=rois, expected_index=0, timeout=15, description="initial tracing ROI")

    passes = {}
    for mode in ("locked", "unlocked"):
        want_locked = mode == "locked"
        is_locked = child.evaluate_js("Boolean(nd2Translucent.state.locked)")
        if is_locked != want_locked:
            child.evaluate_js("document.getElementById('overlay-lock').click(); true")
            wait_for(
                lambda: child.evaluate_js(
                    f"nd2Translucent.state.locked === {str(want_locked).lower()} && !nd2Translucent.state.pending"
                ),
                timeout=15,
                description=f"overlay {mode} toggle",
            )

        _reset_channel_luts(0, 255, 1)
        _wait_for_intensity(parent, 64.0, is_pane=True, timeout=10, description=f"main reset 64 ({mode})")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if _within(sample_canvas_patch(child, *CENTER, is_pane=False), 64.0):
                break
            time.sleep(0.05)

        before_cam = camera(child)
        before_frame = native_state(child)["frame"]
        before_opacity = bridge_result(child, "pywebview.api.translucent_context()")["opacity"]

        drag = _run_continuous_drag_sequence(child, hold_seconds=1.0, release_seconds=2.5)

        after_cam = camera(child)
        after_frame = native_state(child)["frame"]
        after_opacity = bridge_result(child, "pywebview.api.translucent_context()")["opacity"]
        after_locked = child.evaluate_js("Boolean(nd2Translucent.state.locked)")
        invariance_errors = []
        try:
            check_camera(after_cam, before_cam, description=f"{mode} live-LUT camera")
        except AssertionError as exc:
            invariance_errors.append(str(exc))
        if after_cam["rotation"] != before_cam["rotation"] or after_cam["flip"] != before_cam["flip"]:
            invariance_errors.append(f"{mode}: orientation changed {before_cam} -> {after_cam}")
        if after_frame != before_frame:
            invariance_errors.append(f"{mode}: native frame changed {before_frame} -> {after_frame}")
        if not math.isclose(after_opacity, before_opacity, abs_tol=1e-4):
            invariance_errors.append(f"{mode}: opacity changed {before_opacity} -> {after_opacity}")
        if after_locked != want_locked:
            invariance_errors.append(f"{mode}: lock state changed to {after_locked}")
        try:
            roi_after = check_roi_boxes(child, expected_rois=rois, expected_index=0,
                                        timeout=10, description=f"{mode} live-LUT ROI")
        except Exception as exc:
            roi_after = None
            invariance_errors.append(f"{mode}: ROI check failed: {exc}")

        passes[mode] = {
            **drag,
            "camera_invariant": not any("camera" in e or "orientation" in e for e in invariance_errors),
            "frame_invariant": after_frame == before_frame,
            "lock_preserved": after_locked == want_locked,
            "roi_max_error_px": roi_after["maximum_corner_error_css_px"] if roi_after else None,
            "invariance_errors": invariance_errors,
            "ok": drag["ok"] and not invariance_errors,
        }

    ok = all(p["ok"] for p in passes.values())
    errors = []
    for mode, data in passes.items():
        if not data["ok"]:
            errors.append(
                f"{mode}: main={data['main_intensities']} overlay={data['overlay_intensities']} "
                f"invariance={data['invariance_errors']}"
            )
    return {"ok": ok, "passes": passes, "error": "; ".join(errors) if errors else None}


def _poll_rgb_predicate(child, main_pred, overlay_pred, *, timeout=2.5):
    t0 = time.monotonic()
    deadline = t0 + timeout
    main_ok = overlay_ok = False
    main_s = overlay_s = None
    while True:
        main_s = sample_canvas_patch(parent, *CENTER, is_pane=True)
        overlay_s = sample_canvas_patch(child, *CENTER, is_pane=False)
        main_ok = main_s["min_alpha"] == 255 and main_pred(main_s["mean_rgb"])
        overlay_ok = overlay_s["min_alpha"] == 255 and overlay_pred(overlay_s["mean_rgb"])
        if main_ok and overlay_ok:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return {
        "main_ok": main_ok,
        "overlay_ok": overlay_ok,
        "main_rgb": [round(v, 2) for v in main_s["mean_rgb"]],
        "main_min_alpha": main_s["min_alpha"],
        "overlay_rgb": [round(v, 2) for v in overlay_s["mean_rgb"]],
        "overlay_min_alpha": overlay_s["min_alpha"],
        "main_query": _main_query_dict(),
        "overlay_query": _overlay_query_dict(child),
        "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 1),
    }


def _select_tab(name_substring):
    """Click the shell tab whose label contains name_substring."""
    wait_for(
        lambda: parent.evaluate_js(
            f"(() => {{"
            f" const tabs = [...document.querySelectorAll('#tabbar [role=\"tab\"]')];"
            f" const match = tabs.find(t => (t.textContent || '').includes({json.dumps(name_substring)}));"
            f" if (!match) return false;"
            f" match.click();"
            f" return true;"
            f"}})()"
        ),
        timeout=20,
        description=f"shell tab matching {name_substring!r}",
    )


def check_4_controls_and_defaults():
    """Check 4: gamma, toggle/solo, swatch recolor, and restore defaults on 2-channel fixture."""
    _select_tab("cal_2ch")
    wait_for(
        lambda: _pane_eval(
            "!!(state.info && state.info.channels.length === 2 && state.annReady && "
            "state.viewer && state.viewer.isOpen() && "
            "state.viewer.viewport.getContainerSize().x > 200 && "
            "state.viewer.world.getItemAt(0)?.getFullyLoaded())"
        ),
        timeout=20,
        description="2-channel calibration slide ready",
    )
    # Re-home after unhiding the tab so captureTranslucentView hands off a full-size viewport.
    _pane_eval("state.viewer.viewport.goHome(true); state.viewer.forceRedraw(); true")
    wait_for(
        lambda: _pane_eval("state.viewer.world.getItemAt(0)?.getFullyLoaded()"),
        timeout=15,
        description="2-channel calibration tiles after goHome",
    )
    _ensure_channels_open(2)

    click_shell_cloud(parent)
    overlay = wait_for(lambda: next(iter(api._translucent_windows.values()), None),
                       timeout=20, description="2-channel overlay")
    child = overlay._window
    try:
        wait_for(lambda: child.evaluate_js("!!window.nd2Translucent?.state.ready"),
                 timeout=20, description="2-channel overlay ready")
        wait_for(lambda: overlay._configured, timeout=20, description="2-channel overlay configured")
        wait_for(lambda: child.evaluate_js("nd2Translucent.viewer.world.getItemAt(0).getFullyLoaded()"),
                 timeout=20, description="2-channel overlay tiles")

        initial_main_q = _main_query_dict()
        initial_ov_q = _overlay_query_dict(child)
        initial_g = initial_main_q.get("g")
        initial_ov_cam = camera(child)
        initial_ov_geom = child.evaluate_js(
            "(() => { const v = nd2Translucent.viewer; const s = v.viewport.getContainerSize();"
            " return { size: [s.x, s.y], canvas: [v.drawer.canvas.width, v.drawer.canvas.height], dpr: devicePixelRatio }; })()"
        )
        baseline = _poll_rgb_predicate(
            child,
            lambda rgb: all(abs(c - 63.0) <= 10.0 for c in rgb),
            lambda rgb: all(abs(c - 63.0) <= 10.0 for c in rgb),
            timeout=5.0,
        )

        # 4a. Gamma edit on CalA (channel 0) -> gamma ~2.0 -> G brightens (~127) while R, B stay ~63.
        _dispatch_gamma_drag(0, 2.0)
        gamma_res = _poll_rgb_predicate(
            child,
            lambda rgb: rgb[1] >= 105.0 and abs(rgb[0] - 63.0) <= 12.0 and abs(rgb[2] - 63.0) <= 12.0,
            lambda rgb: rgb[1] >= 105.0 and abs(rgb[0] - 63.0) <= 12.0 and abs(rgb[2] - 63.0) <= 12.0,
        )
        gamma_res["query_ok"] = (
            "win" in gamma_res["main_query"]
            and "win" in gamma_res["overlay_query"]["params"]
        )
        gamma_res["ok"] = gamma_res["main_ok"] and gamma_res["overlay_ok"] and gamma_res["query_ok"]

        # 4b. Toggle off CalB (channel 1) -> c=0 -> R,B ~0, G >= 105.
        _pane_eval("document.querySelectorAll('.channel-row [role=\"switch\"]')[1].click(); true")
        toggle_res = _poll_rgb_predicate(
            child,
            lambda rgb: rgb[0] <= 10.0 and rgb[2] <= 10.0 and rgb[1] >= 105.0,
            lambda rgb: rgb[0] <= 10.0 and rgb[2] <= 10.0 and rgb[1] >= 105.0,
        )
        toggle_res["query_ok"] = (
            toggle_res["main_query"].get("c") == "0"
            and toggle_res["overlay_query"]["params"].get("c") == "0"
        )
        toggle_res["ok"] = toggle_res["main_ok"] and toggle_res["overlay_ok"] and toggle_res["query_ok"]

        # 4c. Option/Alt-click solo CalB (channel 1) -> c=1 -> R,B ~63, G ~0.
        _pane_eval("""(() => {
          const sw = document.querySelectorAll('.channel-row [role="switch"]')[1];
          sw.dispatchEvent(new MouseEvent('click', { altKey: true, bubbles: true, cancelable: true }));
          return true;
        })()""")
        solo_res = _poll_rgb_predicate(
            child,
            lambda rgb: rgb[1] <= 10.0 and abs(rgb[0] - 63.0) <= 12.0 and abs(rgb[2] - 63.0) <= 12.0,
            lambda rgb: rgb[1] <= 10.0 and abs(rgb[0] - 63.0) <= 12.0 and abs(rgb[2] - 63.0) <= 12.0,
        )
        solo_res["query_ok"] = (
            solo_res["main_query"].get("c") == "1"
            and solo_res["overlay_query"]["params"].get("c") == "1"
        )
        solo_res["ok"] = solo_res["main_ok"] and solo_res["overlay_ok"] and solo_res["query_ok"]

        # Re-enable all channels (Alt-click soloed CalB again).
        _pane_eval("""(() => {
          const sw = document.querySelectorAll('.channel-row [role="switch"]')[1];
          sw.dispatchEvent(new MouseEvent('click', { altKey: true, bubbles: true, cancelable: true }));
          return true;
        })()""")

        # 4d. Swatch recolor CalA (channel 0) to Red (#FF3B30) -> R jumps (>155), G drops (<50).
        _pane_eval("""(() => {
          document.querySelectorAll('.channel-row button.swatch')[0].click();
          const preset = document.querySelector('.swatch-popover .swatch-preset[data-hex="FF3B30"]');
          if (preset) preset.click();
          return Boolean(preset);
        })()""")
        recolor_res = _poll_rgb_predicate(
            child,
            lambda rgb: rgb[0] >= 155.0 and rgb[1] <= 50.0,
            lambda rgb: rgb[0] >= 155.0 and rgb[1] <= 50.0,
        )
        recolor_res["query_ok"] = (
            str(recolor_res["main_query"].get("col", "")).upper().startswith("FF3B30")
            and str(recolor_res["overlay_query"]["params"].get("col", "")).upper().startswith("FF3B30")
        )
        recolor_res["ok"] = recolor_res["main_ok"] and recolor_res["overlay_ok"] and recolor_res["query_ok"]

        # 4e. Restore defaults: Reset window/gamma (↺) + Reset to metadata color.
        _pane_eval("""(() => {
          document.querySelectorAll('.channel-row button.lut-reset')[0].click();
          if (!document.querySelector('.swatch-popover .swatch-reset-btn')) {
            document.querySelectorAll('.channel-row button.swatch')[0].click();
          }
          const btn = document.querySelector('.swatch-popover .swatch-reset-btn');
          if (btn) btn.click();
          return Boolean(btn);
        })()""")
        restore_res = _poll_rgb_predicate(
            child,
            lambda rgb: all(abs(c - 63.0) <= 10.0 for c in rgb),
            lambda rgb: all(abs(c - 63.0) <= 10.0 for c in rgb),
        )
        ov_params = restore_res["overlay_query"]["params"]
        main_params = restore_res["main_query"]
        restore_res["query_ok"] = (
            not any(k in main_params for k in ("c", "win", "col"))
            and not any(k in ov_params for k in ("c", "win", "col"))
            and (initial_g is None or ov_params.get("g") == initial_g)
        )
        restore_res["ok"] = restore_res["main_ok"] and restore_res["overlay_ok"] and restore_res["query_ok"]

        sub = {
            "baseline": baseline,
            "gamma": gamma_res,
            "toggle": toggle_res,
            "solo": solo_res,
            "recolor": recolor_res,
            "restore_defaults": restore_res,
        }
        failed = [k for k in ("gamma", "toggle", "solo", "recolor", "restore_defaults") if not sub[k]["ok"]]
        return {
            "ok": not failed,
            "initial_g": initial_g,
            "initial_overlay_query": initial_ov_q,
            "initial_overlay_camera": initial_ov_cam,
            "initial_overlay_geom": initial_ov_geom,
            "subchecks": sub,
            "error": f"Failed subchecks: {failed}" if failed else None,
        }
    finally:
        if not overlay._closed:
            overlay.close_translucent()
        wait_for(lambda: not api._translucent_windows and child not in webview.windows,
                 timeout=15, description="2-channel overlay close")


def run():
    try:
        context = api.window_context()
        check(context["role"] == "agent" and context["id"] == session.id, "Wrong Agent window identity")
        wait_for(lambda: parent.evaluate_js("!!window.pywebview?.api"), description="parent bridge")
        bridged = bridge_result(parent, "pywebview.api.window_context()")
        check(bridged["role"] == "agent" and bridged["id"] == session.id, "Parent bridge identity mismatch")
        report["bridge_identity_verified"] = True

        cal1_path, cal2_path = build_fixtures()
        api._httpd, url = app.start_server([cal1_path, cal2_path], window_session=session)
        parent.load_url(url)
        _select_tab("cal_1ch")

        wait_for(
            lambda: _pane_eval(
                "!!(state.info && state.info.channels.length === 1 && state.annReady && "
                "state.viewer && state.viewer.isOpen() && state.viewer.world.getItemAt(0)?.getFullyLoaded())"
            ),
            timeout=20,
            description="1-channel calibration slide ready",
        )
        _ensure_channels_open(1)
        _reset_channel_luts(0, 255, 1)
        wait_for(
            lambda: _within(sample_canvas_patch(parent, *CENTER, is_pane=True), 64.0),
            timeout=10,
            description="1-channel initial baseline 64",
        )

        click_shell_cloud(parent)
        overlay = wait_for(lambda: next(iter(api._translucent_windows.values()), None),
                           timeout=20, description="1-channel overlay")
        child = overlay._window
        try:
            wait_for(lambda: child.evaluate_js("!!window.nd2Translucent?.state.ready"),
                     timeout=20, description="1-channel overlay ready")
            wait_for(lambda: overlay._configured, timeout=20, description="1-channel overlay configured")
            wait_for(lambda: child.evaluate_js("nd2Translucent.viewer.world.getItemAt(0).getFullyLoaded()"),
                     timeout=20, description="1-channel overlay tiles")

            report["checks"]["continuous_drag"] = check_1_continuous_drag(child)
            report["checks"]["slow_tiles"] = check_2_slow_tiles(child)
            report["checks"]["tracing_alignment"] = check_3_tracing_alignment(child)
        finally:
            if not overlay._closed:
                overlay.close_translucent()
            wait_for(lambda: not api._translucent_windows and child not in webview.windows,
                     timeout=15, description="1-channel overlay close")

        report["checks"]["controls_and_defaults"] = check_4_controls_and_defaults()
        report["ok"] = all(bool(item.get("ok")) for item in report["checks"].values())
        if not report["ok"]:
            failed = {k: v.get("error") for k, v in report["checks"].items() if not v.get("ok")}
            report["error"] = f"Live LUT check(s) failed: {failed}"
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        write_report()
        api._close_translucent_windows()
        parent.destroy()


try:
    webview.start(run, private_mode=True)
except Exception:
    report["error"] = traceback.format_exc()
    write_report()
finally:
    if api._httpd is not None:
        api.stop_server_for_update()
    session.mark_closed()
