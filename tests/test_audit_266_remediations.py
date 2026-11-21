"""Regression tests for the 2.6.7 multi-domain audit remediations (Domains 1-5)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import plistlib
import struct
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pytest
import tifffile
import yaml
from node_host import run_node

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Domain 1: Server, Security, Storage, Cache, Annotations & Exports
# ---------------------------------------------------------------------------


def test_zarr_v2_blocks_unsafe_codecs_filters_oversized_chunks_and_zlib_bombs(tmp_path):
    from nd2wsi.storage import single_file, zarr_v2

    base = {
        "zarr_format": 2,
        "shape": [1, 8, 8],
        "chunks": [1, 4, 4],
        "dtype": "<u2",
        "fill_value": 0,
        "order": "C",
        "filters": None,
        "compressor": None,
    }

    for bad in (
        {**base, "compressor": {"id": "pickle"}},
        {**base, "filters": [{"id": "pickle"}]},
        {**base, "chunks": [1, 8192, 8192]},  # 128 MiB > 64 MiB MAX_CHUNK_BYTES
        {**base, "dtype": "|O"},
    ):
        raw = json.dumps(bad).encode("utf-8")
        with pytest.raises(zarr_v2.UnsafeZarrArrayError):
            zarr_v2.ZarrV2RegionReader.open("0", {"0/.zarray": raw}.get)

    # Bounded zlib decompression rejects an oversized payload before full expansion.
    bomb = zlib.compress(b"\x00" * (1024 * 1024))
    with pytest.raises(ValueError, match="decoded chunk size"):
        zarr_v2._decode_chunk_bounded(object(), "zlib", bomb, expected_bytes=64)

    # Eager validation on open_zarr_group(mode="r") rejects unsafe .zarray in SQLiteStore.
    db_path = tmp_path / "unsafe.nd2svs"
    with single_file.SQLiteStore(db_path, read_only=False, prefix="store.ome.zarr") as store:
        store.write_bytes(".zgroup", b'{"zarr_format": 2}')
        store.write_bytes("0/.zarray", json.dumps({**base, "compressor": {"id": "pickle"}}).encode())
    with pytest.raises(zarr_v2.UnsafeZarrArrayError):
        single_file.open_zarr_group(db_path, mode="r")


def test_sqlite_store_pins_inode_across_atomic_replacement(tmp_path):
    from nd2wsi.storage import single_file

    path = tmp_path / "cache.nd2svs"
    with single_file.SQLiteStore(path, read_only=False, prefix="store.ome.zarr") as store:
        store.write_bytes("key.txt", b"original-payload")

    reader = single_file.SQLiteStore(path, read_only=True, prefix="store.ome.zarr")
    try:
        assert reader.read_bytes("key.txt") == b"original-payload"
        # Atomically replace the file on disk with a different store.
        replacement = tmp_path / "replacement.nd2svs"
        with single_file.SQLiteStore(replacement, read_only=False, prefix="store.ome.zarr") as store:
            store.write_bytes("key.txt", b"replaced-payload")
        replacement.replace(path)
        # Pinned reader must never silently switch to the replacement inode.
        assert reader.read_bytes("key.txt") == b"original-payload"
    finally:
        reader.close()


def test_server_hides_version_and_rejects_unauthenticated_expect_100():
    import http.client
    import threading

    from nd2wsi.server import create_server

    server = create_server([], host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        try:
            conn.request("OPTIONS", "/wrong-token/api/slides")
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 404
            assert resp.getheader("Server") == "nd2wsi"
            assert b"BaseHTTP" not in body and b"Python" not in body
        finally:
            conn.close()

        # Expect: 100-continue on an unauthenticated path must be rejected with 404 (no 100).
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        try:
            conn.request(
                "POST",
                "/wrong-token/api/open",
                body=b"{}",
                headers={"Expect": "100-continue", "Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 404
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_cache_sweep_honors_grace_period_for_empty_lockfile(tmp_path):
    from nd2wsi.cache import CacheLock, sweep_stale_builds

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    fresh_lock = CacheLock(cache_dir / "slideA.nd2svs").path
    fresh_lock.write_text("", encoding="utf-8")
    fresh_stage = cache_dir / "slideA.nd2svs.building-999991"
    fresh_stage.mkdir()

    old_lock = CacheLock(cache_dir / "slideB.nd2svs").path
    old_lock.write_text("", encoding="utf-8")
    old_stage = cache_dir / "slideB.nd2svs.building-999992"
    old_stage.mkdir()
    stale_ts = time.time() - 120
    os.utime(old_lock, (stale_ts, stale_ts))

    swept = sweep_stale_builds(cache_dir)
    assert swept == 1
    assert fresh_stage.exists()
    assert not old_stage.exists()


def test_export_calibration_validation_and_roi_reference_bomb(tmp_path, monkeypatch):
    from PIL import Image

    from nd2wsi import export_nd2, render, roi_list

    assert export_nd2._scalar_calibration((float("nan"), 0.5)) is None
    assert export_nd2._scalar_calibration((0.5, float("inf"))) is None
    assert export_nd2._scalar_calibration((-0.5, -0.5)) is None
    assert export_nd2._scalar_calibration((0.325, 0.325)) == pytest.approx(0.325)

    arr = np.zeros((1, 8, 8), dtype=np.uint16)
    buf = io.BytesIO()
    render.export_roi_tiff(
        {"0": arr},
        {
            "nd2wsi": {
                "source": "slide.nd2",
                "tile": 16,
                "dtype": "uint16",
                "rgb": False,
                "levels": [{"path": "0", "downsample": 1, "height": 8, "width": 8}],
                "pixel_size_um": [float("nan"), 0.5],
            },
            "omero": {
                "channels": [{"label": "Ch0", "color": "FFFFFF", "window": {"start": 0, "end": 100}}]
            },
        },
        buf,
        level=0,
        x=0,
        y=0,
        w=8,
        h=8,
        channels=[0],
    )
    assert buf.getvalue()  # Non-finite calibration skipped safely instead of writing NaN resolution

    def _raise_bomb(*_a, **_kw):
        raise Image.DecompressionBombError("too many pixels")

    ref_jpg = tmp_path / "ref.jpg"
    Image.new("RGB", (8, 8)).save(ref_jpg, format="JPEG")
    roi_txt = tmp_path / "ref.txt"
    roi_txt.write_text("1,2\n3,4\n", encoding="utf-8")
    monkeypatch.setattr(Image, "open", _raise_bomb)
    with pytest.raises(ValueError, match="exceed safety limits"):
        roi_list.load_roi_file(roi_txt, width=100, height=100, image_path=ref_jpg)


# ---------------------------------------------------------------------------
# Domain 2: Readers, Rendering, Shading & Plate
# ---------------------------------------------------------------------------


def test_ometiff_big_endian_raw_and_int8_normalization(tmp_path):
    from nd2wsi import ometiff

    arr = np.arange(64 * 64, dtype=">u2").reshape(64, 64) + 258
    path = tmp_path / "bigendian.tif"
    tifffile.imwrite(path, arr, byteorder=">")
    root, _attrs = ometiff.open_direct(path)
    try:
        got = root["0"][0, :, :]
        np.testing.assert_array_equal(got, arr.astype(np.uint16))
    finally:
        root.close()

    assert ometiff._normalize_dtype(np.dtype(np.int8)) == np.dtype(np.float32)


def test_box_mean_preserves_uint32_and_float64_precision():
    from nd2wsi.direct_common import box_mean

    hi = np.full((1, 4, 4), (1 << 31) + 12345, dtype=np.uint32)
    reduced = box_mean(hi, 2)
    assert reduced.dtype == np.uint32
    assert int(reduced[0, 0, 0]) == (1 << 31) + 12345

    f64 = np.full((1, 4, 4), 1e12 + 0.25, dtype=np.float64)
    reduced_f64 = box_mean(f64, 2)
    assert reduced_f64.dtype == np.float64
    assert float(reduced_f64[0, 0, 0]) == pytest.approx(1e12 + 0.25)


def test_histograms_handle_float32_and_extreme_ranges():
    from nd2wsi import render

    # Extreme float32 range must not overflow high - low to inf/NaN
    extreme = np.array([-3e38, 0.0, 3e38], dtype=np.float32).reshape(1, 1, 3)
    attrs_ext = {
        "nd2wsi": {
            "levels": [{"path": "0", "downsample": 1, "height": 1, "width": 3}],
            "dtype": "float32",
        },
        "omero": {
            "channels": [{"label": "Ch0", "color": "FFFFFF", "window": {"start": 0, "end": 1}}]
        },
    }
    h_ext = render.compute_histograms({"0": extreme}, attrs_ext, bins=16)
    assert len(h_ext[0]["bins"]) == 16 and sum(h_ext[0]["bins"]) > 0

    # Bright H-corrected float32 values > 65535 must not be crushed into a [0, 65535] ceiling
    bright = np.linspace(0.0, 90000.0, 64 * 64, dtype=np.float32).reshape(1, 64, 64)
    attrs_bright = {
        "nd2wsi": {
            "levels": [{"path": "0", "downsample": 1, "height": 64, "width": 64}],
            "dtype": "uint16",
        },
        "omero": {
            "channels": [{"label": "Ch0", "color": "FFFFFF", "window": {"start": 0, "end": 65535}}]
        },
    }
    hists = render.compute_histograms({"0": bright}, attrs_bright, bins=32)
    assert hists[0]["vmax"] >= 90000.0
    assert hists[0]["bins"][-1] < 64 * 64


def test_h_adaptive_lattice_enumeration_keeps_negative_origins():
    from nd2wsi.h_adaptive import enumerate_all_lattice_cells

    raw8 = np.full((1, 64, 64), 1000.0, dtype=np.float32)
    raw32 = np.full((1, 16, 16), 1000.0, dtype=np.float32)
    # Negative-sign reciprocal basis produces negative phase coordinates (r_min < 0).
    basis = np.array([[-0.25, 0.0], [0.0, -0.25]], dtype=np.float64)
    cells, (r_min, _r_max), (_c_min, _c_max) = enumerate_all_lattice_cells(
        raw8, raw32, basis, {"BF": 0}, bf_reference=1000.0, saturation=65535.0,
        train_box_ov32=[0, 0, 16, 16],
    )
    assert r_min < 0
    assert any(cell["row"] < 0 or cell["column"] < 0 for cell in cells)


# ---------------------------------------------------------------------------
# Domain 3: Desktop, Lifecycle, Sparkle & Metal
# ---------------------------------------------------------------------------


def test_all_pywebview_file_type_filters_parse_cleanly():
    import re

    import webview.util

    for rel in ("nd2wsi/translucent_window.py", "nd2wsi/app.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        for match in re.finditer(r'"([^"\n]+\(\*\.[A-Za-z0-9_;*.]+\))"', src):
            pattern = match.group(1)
            desc, exts = webview.util.parse_file_type(pattern)
            assert desc and exts, f"Failed parsing {pattern!r} in {rel}"


def test_update_card_retires_stale_probing_replies_and_waits_on_loaded():
    from nd2wsi.update_card import UpdateCardSession

    replies: list[tuple[str, object]] = []
    fallbacks: list[str] = []

    class FakeChannel:
        def __init__(self):
            self.pending: list[tuple[str, object]] = []

        def submit(self, script, complete=None):
            self.pending.append((script, complete))

    class FakeItem:
        def displayVersionString(self):
            return "2.6.7"

        def versionString(self):
            return "2.6.7"

        def itemDescription(self):
            return ""

        def releaseNotesURL(self):
            return None

    class FakeStandard:
        def showUpdateFoundWithAppcastItem_state_reply_(self, *a):
            fallbacks.append("found")

    channel = FakeChannel()
    session = UpdateCardSession(
        FakeStandard(),
        window=None,
        main=lambda fn: fn(),
        channel=channel,
        later=lambda _ms, _fn: (lambda: None),
        current_version="2.6.6",
    )
    session.before_load()
    session.dispatch(
        "showUpdateFoundWithAppcastItem_state_reply_",
        FakeItem(),
        None,
        lambda choice: replies.append(("first", choice)),
    )
    assert session.mode == "probing"
    assert sum(r.block is not None for r in session.replies) == 1

    # page_loaded() probing with None answer (before inject_pywebview finishes) must NOT fall back.
    session.page_loaded()
    _, loaded_complete = channel.pending[-1]
    assert callable(loaded_complete)
    loaded_complete(None)
    assert session.mode == "probing"
    assert fallbacks == []

    # A new phase arriving during probing retires the prior uninvoked reply cleanly.
    session.dispatch(
        "showDownloadInitiatedWithCancellation_",
        lambda: replies.append(("second", "cancel")),
    )
    assert session.replies[0].block is None
    assert sum(r.block is not None for r in session.replies) == 1
    session.page_closed()
    assert replies == [("second", "cancel")]


def test_window_lifecycle_shares_browser_tail_across_controllers():
    from types import SimpleNamespace

    from nd2wsi.window_lifecycle import (
        UpdateShutdownCoordinator,
        WindowCloseCoordinator,
    )

    api = SimpleNamespace()
    window = SimpleNamespace()
    close_coord = WindowCloseCoordinator(api, window, logger=lambda _m: None)
    upd_coord = UpdateShutdownCoordinator(api, window, logger=lambda _m: None)
    assert close_coord._ownership() is upd_coord._ownership()
    assert hasattr(close_coord._ownership(), "_browser_lock")
    assert hasattr(close_coord._ownership(), "_browser_tail")


def test_metal_native_m_and_desktop_updates_gating():
    native_m = (ROOT / "nd2wsi/metal_viewport/native.m").read_text(encoding="utf-8")
    assert "applicationShouldTerminate:" in native_m
    assert "NSTerminateCancel" in native_m
    assert 'commands[@"updates"]' in native_m

    desktop_src = (ROOT / "nd2wsi/desktop.py").read_text(encoding="utf-8")
    assert "ND2WSI_DISABLE_UPDATES" in desktop_src
    assert 'role != "agent"' in desktop_src


# ---------------------------------------------------------------------------
# Domain 4: Frontend JS, HTML & CSS
# ---------------------------------------------------------------------------


def test_frontend_nice_length_move_annotation_and_cancel_update_js():
    app_js = (ROOT / "nd2wsi/static/app.js").read_text(encoding="utf-8")
    nice_start = app_js.index("function niceLength(")
    nice_end = app_js.index("\n/* ---- tools:", nice_start)
    move_start = app_js.index("function moveAnnotation(")
    move_end = app_js.index("\nfunction removeAnnotation(", move_start)
    cancel_start = app_js.index("function cancelUpdatePreparation(")
    cancel_end = app_js.index("\nfunction loadAnnotations(", cancel_start)

    script = (
        app_js[nice_start:nice_end]
        + "\n"
        + app_js[move_start:move_end]
        + "\n"
        + app_js[cancel_start:cancel_end]
        + r"""
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const state = {
  quitPreparing: false,
  quitRequestId: 'r1',
  annDirty: true,
  info: { width: 1000, height: 1000 },
  annotations: [{ id: 'p1', type: 'pin', x: 50, y: 60 }],
};
let scheduled = 0;
function updateAnnotationRecoveryNotice() {}
function annotationRecoveryDrafts() { return [{ draftPath: '/tmp/draft.json' }]; }
function annotationDraftAccepted() { return false; }
function scheduleAnnSave() { scheduled += 1; }
function pushUndoSnapshot() {}
function renderAnnotations() {}
function rebuildAnnList() {}
function annotationsChanged() {}

const res = {
  niceSub: niceLength(0.03),
  niceBig: niceLength(1500000),
  niceZero: niceLength(0),
  niceNaN: niceLength(NaN),
  moveNanOk: moveAnnotation('p1', 10, NaN),
  pinAfterNan: [state.annotations[0].x, state.annotations[0].y],
  cancelOk: cancelUpdatePreparation('r1'),
  scheduledWithDrafts: scheduled,
};
process.stdout.write(JSON.stringify(res));
"""
    )
    out = json.loads(run_node(script, check=True).stdout)
    assert out["niceSub"] == pytest.approx(0.02)
    assert out["niceBig"] == pytest.approx(1000000)
    assert out["niceZero"] == 0
    assert out["niceNaN"] == 0
    assert out["moveNanOk"] is False
    assert out["pinAfterNan"] == [50, 60]
    assert out["cancelOk"] is True
    assert out["scheduledWithDrafts"] == 0


def test_frontend_select_scene_reentrancy_and_static_guards():
    app_js = (ROOT / "nd2wsi/static/app.js").read_text(encoding="utf-8")
    shell_js = (ROOT / "nd2wsi/static/shell-v1.js").read_text(encoding="utf-8")
    shell_css = (ROOT / "nd2wsi/static/native-shell-v1.css").read_text(encoding="utf-8")
    boot_html = (ROOT / "nd2wsi/static/boot.html").read_text(encoding="utf-8")

    # Pointer capture on persistent #ann-layer, not detached hit child
    assert "layer.setPointerCapture?.(pointerId)" in app_js
    assert "layer.releasePointerCapture?.(pointerId)" in app_js
    assert 'layer.addEventListener?.("lostpointercapture", onCancel)' in app_js
    assert "hit.setPointerCapture" not in app_js

    # Early capture guards & CSS/boot/shell hardening
    assert "wireEarlyInputGuards();" in app_js
    assert ".window-actions button[hidden] { display:none; }" in shell_css
    assert "pollGen" in boot_html and "window.pywebview?.api?.open_slide" in boot_html
    assert 'if ($("shading-confirm")?.open) return;' in shell_js
    assert 'zone.addEventListener("pointerdown"' in shell_js
    assert 'if (compare.linked && compare.linkMode === "scale") return;' in shell_js
    assert "linked: compare.linked, linkMode: compare.linkMode, pairs: nextPairs" in shell_js
    assert "close_translucent_viewer" in shell_js[shell_js.index("function receiveSceneSelected("):]

    # Re-entrancy guard in selectScene
    fn_start = app_js.index("async function selectScene(")
    fn_end = app_js.index("\nfunction renderScenePicker(", fn_start + 1)
    script = (
        app_js[fn_start:fn_end]
        + r"""
let resolveFetch;
let fetchCount = 0;
const state = {
  info: { seriesIndex: 0, seriesCount: 3, name: 'slide' },
  quitPreparing: false,
  sceneSwitching: false,
  lutPersistence: { isDirty: () => false, flush: async () => {} },
};
const annotationStore = { flushForUpdate: async () => {} };
function $() { return null; }
function setScenePickerOpen() {}
function renderScenePicker() {}
function showToast() {}
function resolveSceneUrl(d) { return d.url; }
function currentSlideSid() { return 's1'; }
const VIEWPORT_PROTOCOL_VERSION = 2;
const location = { origin: 'http://127.0.0.1:8000' };
async function fetch() {
  fetchCount += 1;
  return new Promise(r => { resolveFetch = () => r({ ok: true, json: async () => ({ sid: 's2', url: '/t/s/s2/' }) }); });
}
(async () => {
  const first = selectScene(1);
  await Promise.resolve();
  const second = await selectScene(2);
  resolveFetch();
  const firstResult = await first;
  process.stdout.write(JSON.stringify({ firstResult, second, fetchCount }));
})();
"""
    )
    out = json.loads(run_node(script, check=True).stdout)
    assert out == {"firstResult": True, "second": False, "fetchCount": 1}


# ---------------------------------------------------------------------------
# Domain 5: Packaging, CI, NSIS Installer & QA
# ---------------------------------------------------------------------------


def test_nsis_upgrade_reparse_normalization_and_uninstfinalize():
    nsi = (ROOT / "packaging/windows-installer.nsi").read_text(encoding="utf-8")
    assert "Var UpgradeExitCode" in nsi
    assert 'ExecWait \'"$PLUGINSDIR\\prev-uninstall.exe" /S _?=$INSTDIR\' $UpgradeExitCode' in nsi
    assert "StrCpy $InitialInstallFresh 1" in nsi[nsi.index("SecApplication.upgrade.complete"):]
    assert "Function ${PREFIX}IsNameSurrogateReparse" in nsi
    assert "0x20000000" in nsi and "FindFirstFileW" in nsi
    assert "Function ${PREFIX}NormalizePath" in nsi and "GetLongPathNameW" in nsi
    assert "!uninstfinalize" in nsi
    # AcquireSetupMutex saves/restores $0 and $1
    mutex_fn = nsi[nsi.index("Function ${PREFIX}AcquireSetupMutex"):nsi.index("Function ${PREFIX}FindRunningApp")]
    assert "Push $0" in mutex_fn and "Push $1" in mutex_fn and "Pop $1" in mutex_fn and "Pop $0" in mutex_fn

    ps1 = (ROOT / "packaging/verify_windows_installer.ps1").read_text(encoding="utf-8")
    assert "Installed Uninstall.exe does not have a valid Authenticode signature" in ps1


def test_workflows_pin_uv_nsis_and_gate_pr_signing():
    for wf in (".github/workflows/ci.yml", ".github/workflows/realdata.yml"):
        doc = yaml.safe_load((ROOT / wf).read_text(encoding="utf-8"))
        for job in doc["jobs"].values():
            for step in job["steps"]:
                if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                    assert step.get("with", {}).get("version") == "0.12.10", f"Unpinned uv in {wf}"

    win_yml = (ROOT / ".github/workflows/windows.yml").read_text(encoding="utf-8")
    assert "choco install nsis" not in win_yml
    assert "ba63dffc4410ee89193e1cb5a41989991bd77c61068da17e3156d136b7b0b3d8" in win_yml
    win_doc = yaml.safe_load(win_yml)
    auth_step = next(
        s for s in win_doc["jobs"]["build"]["steps"] if s.get("id") == "authenticode"
    )
    assert auth_step.get("if") == "github.event_name != 'pull_request'"


def test_build_sdpc_codec_rejects_corrupt_download_before_caching(tmp_path, monkeypatch):
    builder = _load_module("build_sdpc_codec_audit", ROOT / "packaging/build_sdpc_codec.py")
    good = b"valid-tarball"
    source = {
        "filename": "pkg.tar.xz",
        "url": "https://example.invalid/pkg.tar.xz",
        "sha256": hashlib.sha256(good).hexdigest(),
    }

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    # First attempt returns corrupted data; must NOT poison cache, retries and succeeds on attempt 2.
    responses = [_Resp(b"corrupted-mirror-html"), _Resp(good)]
    monkeypatch.setattr(builder.urllib.request, "urlopen", lambda url, timeout: responses.pop(0))
    got = builder.download(source, tmp_path, sleep=lambda _s: None)
    assert got.read_bytes() == good

    # All attempts corrupted -> raises checksum mismatch and leaves cache completely empty.
    bad_dir = tmp_path / "all_bad"
    bad_dir.mkdir()
    monkeypatch.setattr(builder.urllib.request, "urlopen", lambda url, timeout: _Resp(b"bad"))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        builder.download(source, bad_dir, sleep=lambda _s: None)
    assert list(bad_dir.iterdir()) == []


def test_run_native_check_resolves_python_and_rejects_prod_bundle(tmp_path):
    runner = _load_module("run_native_check_audit", ROOT / "qa/run_native_check.py")
    script = ROOT / "qa/check_native_astromap.py"

    # Python interpreter target preserves virtualenv symlink path
    cmd, cwd = runner._resolve_target(Path(sys.executable), script)
    assert cmd == [os.path.abspath(sys.executable), str(script)]
    assert cwd == str(ROOT)

    # Hardened production .app (ND2WSIDevelopmentOnly=False) must fail fast
    prod_app = tmp_path / "nd2wsi-viewer.app"
    (prod_app / "Contents/MacOS").mkdir(parents=True)
    (prod_app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": "nd2wsi-viewer", "ND2WSIDevelopmentOnly": False})
    )
    with pytest.raises(SystemExit, match="ND2WSIDevelopmentOnly"):
        runner._resolve_target(prod_app, script)

    # Dev-only .app (ND2WSIDevelopmentOnly=True) is permitted
    (prod_app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": "nd2wsi-viewer", "ND2WSIDevelopmentOnly": True})
    )
    cmd, cwd = runner._resolve_target(prod_app, script)
    assert cmd[0].endswith("Contents/MacOS/nd2wsi-viewer") and cmd[1] == "-c"
    assert cwd == "/private/tmp"


def test_build_windows_installer_sign_file_mode_fails_closed_without_creds(tmp_path, monkeypatch):
    builder = _load_module("build_win_inst_audit", ROOT / "packaging/build_windows_installer.py")
    for var in (
        "WINDOWS_SIGN_PFX_PATH",
        "WINDOWS_SIGN_PFX_PASSWORD",
        "WINDOWS_SIGN_THUMBPRINT",
        "WINDOWS_SIGNTOOL",
        "WINDOWS_REQUIRE_SIGNING",
    ):
        monkeypatch.delenv(var, raising=False)
    # Minimal valid PE header
    pe = bytearray(0x100)
    pe[0:2] = b"MZ"
    struct.pack_into("<I", pe, 0x3C, 0x80)
    pe[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", pe, 0x84, 0x8664)
    uninst = tmp_path / "Uninstall.exe"
    uninst.write_bytes(bytes(pe))
    with pytest.raises(RuntimeError, match="without Authenticode signing credentials"):
        builder.main(["--sign-file", str(uninst)])
