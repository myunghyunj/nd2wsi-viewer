"""Focused regression tests for v2.6.5 security, stability, format, and UI remediations."""

from __future__ import annotations

import http.client
import json
import math
import os
import struct
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import numpy as np
import pytest
from node_host import run_node

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. packaging/launch.py: reject arbitrary `-c` code execution (A1)
# ---------------------------------------------------------------------------
def test_launcher_rejects_arbitrary_dash_c_code(tmp_path):
    marker = tmp_path / "pwned.txt"
    payload = f"open({str(marker)!r}, 'w').write('x')"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "packaging/launch.py"), "-c", payload],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 2
    assert not marker.exists()
    assert "-c execution is not supported" in proc.stderr


# ---------------------------------------------------------------------------
# 2. nd2wsi/storage/zarr_v2.py: compressor allowlist & chunk byte cap (A2)
# ---------------------------------------------------------------------------
def test_zarr_v2_rejects_pickle_compressor_and_oversized_chunk():
    from nd2wsi.storage.zarr_v2 import MAX_CHUNK_BYTES, ZarrV2RegionReader

    base_meta = {
        "zarr_format": 2,
        "order": "C",
        "shape": [1, 64, 64],
        "chunks": [1, 64, 64],
        "dtype": "<u2",
        "fill_value": 0,
        "filters": None,
        "compressor": {"id": "pickle", "protocol": 5},
    }
    with pytest.raises(ValueError, match="compressor"):
        ZarrV2RegionReader(base_meta, "0", lambda _k: None)

    huge_meta = {
        **base_meta,
        "compressor": None,
        "shape": [1, 10000, 10000],
        "chunks": [1, 10000, 10000],
    }
    assert 10000 * 10000 * 2 > MAX_CHUNK_BYTES
    with pytest.raises(ValueError, match="safety limit"):
        ZarrV2RegionReader(huge_meta, "0", lambda _k: None)


# ---------------------------------------------------------------------------
# 3. nd2wsi/server.py: _gate() Host/Origin/proxy-URI + security headers (A6/A9)
# ---------------------------------------------------------------------------
def test_server_gate_and_security_headers():
    from nd2wsi.server import create_server, server_url

    httpd = create_server([], host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = server_url(httpd)
        parsed = urlparse(base)
        host, port = parsed.hostname, parsed.port
        assert host == "127.0.0.1" and port
        token_prefix = parsed.path.rstrip("/")
        api_path = f"{token_prefix}/api/slides"

        def raw_request(method: str, target: str, headers: list[tuple[str, str]]):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            try:
                conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
                for k, v in headers:
                    conn.putheader(k, v)
                conn.endheaders()
                resp = conn.getresponse()
                resp.read()
                return resp.status, dict(resp.getheaders())
            finally:
                conn.close()

        # Valid request returns 200 with CSP + X-Frame-Options
        status, hdrs = raw_request("GET", api_path, [("Host", f"127.0.0.1:{port}")])
        assert status == 200
        hdrs_lower = {k.lower(): v for k, v in hdrs.items()}
        assert hdrs_lower.get("x-frame-options") == "SAMEORIGIN"
        csp = hdrs_lower.get("content-security-policy", "")
        assert "default-src 'self'" in csp
        assert "script-src 'self' 'unsafe-eval'" in csp
        assert "object-src 'none'" in csp

        # Wrong Host port -> 404 + Connection: close
        other_port = port + 1 if port < 65535 else port - 1
        status, hdrs = raw_request("GET", api_path, [("Host", f"127.0.0.1:{other_port}")])
        assert status == 404
        assert {k.lower(): v for k, v in hdrs.items()}.get("connection") == "close"

        # Cross-port Origin -> 404 + Connection: close
        status, hdrs = raw_request(
            "GET",
            api_path,
            [("Host", f"127.0.0.1:{port}"), ("Origin", f"http://127.0.0.1:{other_port}")],
        )
        assert status == 404
        assert {k.lower(): v for k, v in hdrs.items()}.get("connection") == "close"

        # Multiple Origin headers -> 404
        status, _ = raw_request(
            "GET",
            api_path,
            [
                ("Host", f"127.0.0.1:{port}"),
                ("Origin", f"http://127.0.0.1:{port}"),
                ("Origin", f"http://127.0.0.1:{port}"),
            ],
        )
        assert status == 404

        # Absolute proxy-form request URI -> 404
        status, _ = raw_request(
            "GET",
            f"http://127.0.0.1:{port}{api_path}",
            [("Host", f"127.0.0.1:{port}")],
        )
        assert status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 4. nd2wsi/window_chrome.py: capability token redaction (A13)
# ---------------------------------------------------------------------------
def test_window_chrome_redacts_capability_token():
    from nd2wsi.window_chrome import _redact_url_for_log

    secret = "SecretTokenValue123456"
    redacted = _redact_url_for_log(f"http://127.0.0.1:8123/{secret}/s/abc/?c=0&win=1:2")
    assert secret not in redacted
    assert "<redacted>" in redacted


# ---------------------------------------------------------------------------
# 5. nd2wsi/cache_removal.py: rescue_annotations refuses symlinks (A8)
# ---------------------------------------------------------------------------
def test_rescue_annotations_rejects_symlink(tmp_path):
    from nd2wsi.cache_removal import rescue_annotations

    cache_dir = tmp_path / "slide.nd2wsi"
    cache_dir.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text('{"secret": true}', encoding="utf-8")
    link = cache_dir / "annotations_s0.json"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable")
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises((OSError, ValueError)):
        rescue_annotations(cache_dir, home)
    assert list(home.iterdir()) == []


# ---------------------------------------------------------------------------
# 6. nd2wsi/migration.py: pack_directory rejects escaping relative_path (A8)
# ---------------------------------------------------------------------------
def test_migration_pack_directory_rejects_escaping_relative_path(tmp_path):
    from nd2wsi.cache import MANIFEST_FORMAT, MANIFEST_NAME
    from nd2wsi.migration import pack_directory

    outside = tmp_path / "outside.nd2"
    outside.write_bytes(b"fake-nd2")
    slide_dir = tmp_path / "slides"
    slide_dir.mkdir()
    cache_dir = slide_dir / "slide.nd2wsi"
    cache_dir.mkdir()
    manifest = {
        "format": MANIFEST_FORMAT,
        "kind": "full",
        "complete": True,
        "source": {
            "name": "outside.nd2",
            "relative_path": "../../outside.nd2",
            "size": outside.stat().st_size,
        },
    }
    (cache_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    target = slide_dir / "slide.nd2svs"
    with pytest.raises(ValueError, match="relative source path"):
        pack_directory(cache_dir, target)
    assert not target.exists()


# ---------------------------------------------------------------------------
# 7. nd2wsi/ui_settings.py: Agent isolation on record_recent_file (A7)
# ---------------------------------------------------------------------------
def test_agent_record_recent_file_does_not_touch_user_settings(tmp_path, monkeypatch):
    from nd2wsi import ui_settings

    monkeypatch.setattr(ui_settings, "ui_settings_root", lambda: tmp_path)
    settings_file = ui_settings.ui_settings_path()
    slide = tmp_path / "sample.nd2"
    slide.write_bytes(b"nd2")

    assert ui_settings.record_recent_file(slide, window_role="agent") == []
    assert ui_settings.record_recent_file(slide, role="agent") == []
    assert not settings_file.exists()


# ---------------------------------------------------------------------------
# 8. nd2wsi/release_selection.py: prefers Windows -setup.exe over .zip (B11)
# ---------------------------------------------------------------------------
def test_release_selection_prefers_windows_setup_exe():
    from nd2wsi.release_selection import select_release_asset

    releases = [
        {
            "tag_name": "v2.7.0",
            "draft": False,
            "prerelease": False,
            "html_url": "https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.7.0",
            "assets": [
                {
                    "name": "nd2wsi-viewer-2.7.0-windows-x64.zip",
                    "browser_download_url": "https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.7.0/nd2wsi-viewer-2.7.0-windows-x64.zip",
                },
                {
                    "name": "nd2wsi-viewer-2.7.0-windows-x64-setup.exe",
                    "browser_download_url": "https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.7.0/nd2wsi-viewer-2.7.0-windows-x64-setup.exe",
                },
            ],
        }
    ]
    selected = select_release_asset(
        releases, platform_name="win32", machine="x86_64", current_version="2.6.5"
    )
    assert selected is not None
    assert selected["asset_name"] == "nd2wsi-viewer-2.7.0-windows-x64-setup.exe"


# ---------------------------------------------------------------------------
# 9. nd2wsi/translucent_window.py: col color handoff validation (C7/A12)
# ---------------------------------------------------------------------------
def test_translucent_window_validates_col_parameter():
    from nd2wsi import translucent_window as overlay

    store = SimpleNamespace(
        attrs={
            "nd2wsi": {"rgb": False, "levels": [{"width": 1000, "height": 1000}]},
            "omero": {"channels": [{}, {}]},
        },
        generation="g1",
        plate=None,
    )
    base = {
        "version": 1,
        "center": [500, 500],
        "zoom": 1.0,
        "rotation": 0,
        "flip": False,
        "query": "?c=0,1&col=ff00aa,00ff88&g=g1",
    }
    ok = overlay.validate_context(store, base, slide_url="http://local/t/s/abc/")
    assert "col=ff00aa%2C00ff88" in ok["query"]

    for bad_col in ("nothex", "ff00aa,00ff88,112233", "fff", "ff00aa;alert(1)"):
        with pytest.raises(ValueError):
            overlay.validate_context(
                store,
                {**base, "query": f"?c=0&col={bad_col}&g=g1"},
                slide_url="http://local/t/s/abc/",
            )


# ---------------------------------------------------------------------------
# 10. nd2wsi/render.py: parse_channels deduplicates indices (B5)
# ---------------------------------------------------------------------------
def test_parse_channels_deduplicates_and_preserves_order():
    from nd2wsi.render import parse_channels

    assert parse_channels("2,0,2,1,0,2", 4) == [2, 0, 1]
    assert parse_channels("none", 4) == []


# ---------------------------------------------------------------------------
# 11. nd2wsi/h_model.py: template NaN hole & zip archive budget (B6)
# ---------------------------------------------------------------------------
def test_h_model_template_nan_hole_and_zip_budget(tmp_path):
    import warnings

    from nd2wsi import h_model

    grid = np.ones((1, 100, 100), dtype=np.float64)
    grid[0, 45:54, 45:54] = np.nan  # 0.81% NaN hole: passes 99% mean check, fails min(den) <= 1e-6
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(h_model.Unsupported):
            h_model.template(grid, sigma=0.5)

    big_npz = tmp_path / "bomb.npz"
    fields = sorted(h_model.Model.BASE_FIELDS)
    with zipfile.ZipFile(big_npz, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, name in enumerate(fields):
            blob = b"\x00" * (h_model.MAX_MODEL_BYTES + 1024) if i == 0 else b"\x00"
            zf.writestr(f"{name}.npy", blob)
    with pytest.raises(ValueError, match="Oversized"):
        h_model.Model._check_archive_budget(big_npz)


# ---------------------------------------------------------------------------
# 12. nd2wsi/scalebar.py & nd2wsi/ometiff.py: underflow & unnamed Image (B13)
# ---------------------------------------------------------------------------
def test_scalebar_underflow_and_ometiff_unnamed_series():
    from nd2wsi.ometiff import _parse_ome_xml
    from nd2wsi.scalebar import layout

    tiny = math.nextafter(1e-20, 0.0)
    bar = layout(1000, 1000, (tiny, tiny), 1.0)
    assert math.isfinite(bar.width) and bar.width > 0 and math.isfinite(bar.length_um)

    xml = (
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0"><Pixels ID="Pixels:0" SizeX="64" SizeY="64" SizeC="1" SizeZ="1" SizeT="1" Type="uint8"/></Image>'
        '<Image ID="Image:1"><Pixels ID="Pixels:1" SizeX="64" SizeY="64" SizeC="1" SizeZ="1" SizeT="1" Type="uint8"/></Image>'
        "</OME>"
    )
    images = _parse_ome_xml(xml)
    assert [img["name"] for img in images] == ["Series 1", "Series 2"]


# ---------------------------------------------------------------------------
# 13. nd2wsi/czi.py & nd2wsi/lif.py: DTD rejection & header bounds (B4)
# ---------------------------------------------------------------------------
def test_czi_and_lif_reject_dtd_and_oversized_headers(tmp_path):
    from nd2wsi.czi import _MAX_DIR_BYTES, _read_czi_header_and_index
    from nd2wsi.lif import _MAX_XML_BYTES, _read_lif_header_and_blocks

    # LIF with DTD declaration
    dtd_xml = '<!DOCTYPE foo [<!ENTITY x "y">]><LMSDataContainerHeader Version="2"/>'
    dtd_bytes = dtd_xml.encode("utf-16-le")
    lif_dtd = tmp_path / "dtd.lif"
    lif_dtd.write_bytes(struct.pack("<iIBI", 0x70, 0, 0x2A, len(dtd_xml)) + dtd_bytes)
    fd = os.open(lif_dtd, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="DTD|entity"):
            _read_lif_header_and_blocks(fd, lif_dtd.stat().st_size)
    finally:
        os.close(fd)

    # LIF claiming XML > _MAX_XML_BYTES
    lif_big = tmp_path / "big.lif"
    lif_big.write_bytes(struct.pack("<iIBI", 0x70, 0, 0x2A, (_MAX_XML_BYTES // 2) + 100))
    fd = os.open(lif_big, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="bounds"):
            _read_lif_header_and_blocks(fd, lif_big.stat().st_size)
    finally:
        os.close(fd)

    # CZI claiming directory used_size > _MAX_DIR_BYTES
    czi_big = tmp_path / "big.czi"
    file_hdr = bytearray(112)
    file_hdr[0:10] = b"ZISRAWFILE"
    struct.pack_into("<QQ", file_hdr, 84, 112, 0)
    dir_hdr = bytearray(160)
    dir_hdr[0:15] = b"ZISRAWDIRECTORY"
    struct.pack_into("<QQi", dir_hdr, 16, _MAX_DIR_BYTES + 1, _MAX_DIR_BYTES + 1, 1)
    czi_big.write_bytes(bytes(file_hdr) + bytes(dir_hdr))
    fd = os.open(czi_big, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="safety limit"):
            _read_czi_header_and_index(fd, czi_big.stat().st_size)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# 14. Frontend regressions: bounded z-index, clampToStage(0), DOM/CSS,
#     selectScene fail-closed flush, translucent-v1 col/URL validation
# ---------------------------------------------------------------------------
def test_frontend_floating_windows_bounded_z_and_zero_stage_clamp():
    mod = ROOT / "nd2wsi/static/floating-windows-v1.js"
    script = r"""
const {createManager} = require(process.argv[1]);
function node() {
  const classes = new Set();
  return {
    style: {}, children: [], handlers: {}, clientWidth: 0, offsetHeight: 200,
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle(c, on) { if (on === undefined) on = !classes.has(c); on ? classes.add(c) : classes.delete(c); }
    },
    append(n) { this.children.push(n); },
    addEventListener(name, fn) { this.handlers[name] = fn; },
    setPointerCapture() {},
    querySelector(q) { return this.parts[q]; },
    parts: {},
  };
}
const panels = [], stage = {clientWidth: 1000, clientHeight: 700};
const document = {createElement: node, querySelectorAll: () => panels};
const storage = {getItem: () => null, setItem() {}};
const mgr = createManager({stage, document, storage});
function mk(key) {
  const el = node(), btn = node();
  panels.push(el);
  for (const q of ['.win-body', '.win-titlebar', '.tl-close', '.tl-min', '.tl-zoom']) el.parts[q] = node();
  return {el, api: mgr.createWindow(el, {key, def: () => ({x: 20, y: 20, w: 260, h: 200}), minW: 200, minH: 150, maxW: 600, zoomW: 480, toolbarBtn: btn})};
}
const a = mk('a'), b = mk('b');
for (let i = 0; i < 200; i++) {
  (i % 2 === 0 ? a : b).el.handlers.pointerdown({});
}
const maxZ = Math.max(Number(a.el.style.zIndex), Number(b.el.style.zIndex));
const beforeLeft = a.el.style.left;
stage.clientWidth = 0;
stage.clientHeight = 0;
a.api.clampToStage();
process.stdout.write(JSON.stringify({maxZ, unchangedOnZeroStage: a.el.style.left === beforeLeft}));
"""
    res = run_node(script, mod, check=True)
    out = json.loads(res.stdout)
    assert out["maxZ"] < 40
    assert out["unchangedOnZeroStage"] is True


def test_frontend_static_markup_css_and_scene_flush_fail_closed():
    html = (ROOT / "nd2wsi/static/index.html").read_text(encoding="utf-8")
    toolbar_start = html.index('id="toolbar"')
    toolbar_end = html.index("</header>", toolbar_start)
    toolbar_slice = html[toolbar_start:toolbar_end]
    assert 'id="scene-picker-menu"' not in toolbar_slice
    assert 'id="scene-picker-menu"' in html[toolbar_end:]

    shell_css = (ROOT / "nd2wsi/static/native-shell-v1.css").read_text(encoding="utf-8")
    assert "#empty[hidden]" in shell_css

    app_js = (ROOT / "nd2wsi/static/app.js").read_text(encoding="utf-8")
    fn_start = app_js.index("async function selectScene(")
    fn_end = app_js.index("\nfunction renderScenePicker(", fn_start + 1)
    select_scene_src = app_js[fn_start:fn_end]
    script = (
        select_scene_src
        + r"""
let fetchCalled = false, toastMsg = null;
const state = {
  info: {seriesIndex: 0, seriesCount: 2, name: 'slide'},
  quitPreparing: false,
  lutPersistence: { isDirty: () => false, flush: async () => {} },
};
const annotationStore = { flushForUpdate: async () => { throw new Error('disk full'); } };
function $() { return null; }
function setScenePickerOpen() {}
function renderScenePicker() {}
function showToast(m) { toastMsg = m; }
async function fetch() { fetchCalled = true; return {ok: true, json: async () => ({sid: 's2', url: '/tok/s/s2/'})}; }
selectScene(1).then((ok) => {
  process.stdout.write(JSON.stringify({ok, fetchCalled, toastMsg}));
});
"""
    )
    res = run_node(script, check=True)
    out = json.loads(res.stdout)
    assert out["ok"] is False
    assert out["fetchCalled"] is False
    assert out["toastMsg"] and "disk full" in out["toastMsg"]


def test_translucent_js_rejects_bad_slide_url_and_validates_col():
    js = (ROOT / "nd2wsi/static/translucent-v1.js").read_text(encoding="utf-8")
    assert "slide.search" in js and "slide.hash" in js
    assert '"col"' in js or "'col'" in js


def test_windows_installer_keeps_install_button_clickable_and_supports_bundled_webview2():
    nsi = (ROOT / "packaging/windows-installer.nsi").read_text(encoding="utf-8")
    assert "!define MUI_DIRECTORYPAGE_VERIFYONLEAVE" in nsi
    assert "GetInstDirError $0" in nsi
    assert "GetDiskFreeSpaceExW" in nsi
    assert "System::Int64Op" in nsi
    assert "CheckLeafPath" in nsi
    assert "missing_root=1" in nsi
    assert "!ifdef BUNDLED_WEBVIEW2" in nsi
    assert "ExistingUpgrade" in nsi
    builder = (ROOT / "packaging/build_windows_installer.py").read_text(encoding="utf-8")
    assert '"BUNDLED_WEBVIEW2"' in builder

