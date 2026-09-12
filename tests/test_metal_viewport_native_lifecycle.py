"""Explicitly opt-in isolated Agent-window fault tests; never drive User UI.

ND2WSI_TEST_NATIVE_LIFECYCLE=1 enables short-lived synthetic windows on macOS.
No slide, cache, annotation, or installed application is opened or modified.
"""
import json
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or os.environ.get("ND2WSI_TEST_NATIVE_LIFECYCLE") != "1",
    reason="explicit opt-in required: creates isolated synthetic Agent windows",
)


@pytest.mark.parametrize("fault,kind,failure", [
    ("metadata_timeout", "fatal", "metadata_timeout"),
    ("metadata_invalid", "fatal", "metadata_invalid"),
    ("all_tiles_failed", "fatal", "tile_unavailable"),
    ("gpu_fatal", "fatal", "gpu_execution_failure"),
    ("gpu_memory_pressure", "fatal", "memory_pressure"),
    ("tile_503", "closed", None),
    ("http503_exhausted", "fatal", "tile_unavailable"),
    ("single_tile_failed", "closed", None),
    ("manual_handoff", "handoff", None),
    ("close_race", "closed", None),
    ("none", "closed", None),
    ("absent_state", "closed", None),
    ("malformed_state", "closed", None),
])
def test_native_fault_outcome_and_gpu_drain(tmp_path, fault, kind, failure):
    metadata = {"source": "synthetic-diagnostic", "dtype": "uint16", "width": 32, "height": 32,
                "tile_size": 512, "levels": [{"path": "0", "width": 32, "height": 32, "downsample": 1}],
                "channels": [{"label": "Synthetic", "window": [0, 65535], "color": [3, 73, 189]}]}
    if fault == "single_tile_failed":
        metadata["width"] = 1024
        metadata["levels"][0]["width"] = 1024

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            is_metadata = self.path.endswith("metadata")
            status = 200
            body = json.dumps(metadata).encode() if is_metadata else b"\x01\x20" * 32 * min(metadata["width"], 512)
            if not is_metadata and fault == "http503_exhausted":
                status = 503
            if not is_metadata and fault == "single_tile_failed":
                if "tx=1" in self.path:
                    status = 500
                else:
                    time.sleep(.1)  # Failed peer lands before the good image.
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    report = tmp_path / "native.json"
    state = {"version": 1, "source_dimensions": [32, 32], "center": [12, 17], "zoom": 12,
             "channels": [{"window": [11, 60000], "gamma": 2, "color": [3, 73, 189], "visible": False}]}
    if fault == "single_tile_failed":
        state.update(source_dimensions=[1024, 32], center=[512, 16], zoom=.5)
    context = {"id": uuid.uuid4().hex, "role": "agent", "open_attempt_id": uuid.uuid4().hex,
               "fallback_consumed": False, "initial_view_state": state,
               "diagnostics": {"enabled": True, "fault": fault,
                               "close_after_first_presented": fault in ("tile_503", "none", "single_tile_failed", "absent_state", "malformed_state"),
                               "handoff_after_first_presented": fault == "manual_handoff"}}
    if fault == "absent_state":
        context["initial_view_state"] = None
    elif fault == "malformed_state":
        context["initial_view_state"] = {"version": 9}
    library = Path(__file__).resolve().parents[1] / "nd2wsi/metal_viewport/libnd2wsi_viewport.dylib"
    code = """import ctypes,sys
f=ctypes.CDLL(sys.argv[1]).nd2wsi_viewport_run
f.argtypes=[ctypes.c_char_p]*3;f.restype=ctypes.c_int
raise SystemExit(f(*(v.encode() for v in sys.argv[2:])))
"""
    environment = {k: v for k, v in os.environ.items() if not k.startswith("ND2WSI_VIEWPORT_")}
    try:
        result = subprocess.run([sys.executable, "-c", code, str(library),
                                 f"http://127.0.0.1:{server.server_port}/isolated/", json.dumps(context), str(report)],
                                env=environment, capture_output=True, text=True, timeout=25)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert report.exists(), result.stderr
    output = json.loads(report.read_text())
    if output["outcome"].get("failure_kind") == "gpu_device_unavailable":
        pytest.skip("unified-memory Metal device unavailable in test process")
    assert result.returncode == (5 if kind == "fatal" else 0), result.stderr
    assert output["outcome"]["kind"] == kind
    assert output["outcome"].get("failure_kind") == failure
    assert output["lifecycle"]["active_gpu_commands"] == 0
    assert output["lifecycle"]["finalized"] is True
    assert output["open_attempt_id"] == context["open_attempt_id"]
    assert output["fallback_consumed"] is False
    if fault in ("tile_503", "none", "single_tile_failed", "manual_handoff"):
        assert output["outcome"]["first_presented"] is True
        assert output["view_state"] == state
    if fault == "tile_503":
        assert output["backpressure"]["http_503_responses"] == 2
        assert output["backpressure"]["retries_scheduled"] == 2
    if fault == "http503_exhausted":
        assert output["backpressure"]["http_503_responses"] == 6
        assert output["backpressure"]["retries_scheduled"] == 5
        assert output["backpressure"]["exhausted_tile_retries"] == 1
    if fault == "single_tile_failed":
        assert any("Raw tile" in entry["reason"] for entry in output["errors"])
    if fault in ("metadata_timeout", "metadata_invalid", "close_race"):
        assert output["transfers"]["raw_tile_requests"] == 0
    if fault == "absent_state":
        assert output["outcome"]["first_presented"] is True
        assert output["errors"] == []
        assert output["view_state"]["center"] == [16, 16]
    if fault == "malformed_state":
        assert output["outcome"]["first_presented"] is True
        assert [error["reason"] for error in output["errors"]] == ["Invalid initial view state ignored; using fitted view"]
