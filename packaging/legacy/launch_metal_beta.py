"""Separate macOS-only beta entry point; no stable updater or associations."""
import json
import multiprocessing
import os
import plistlib
import sys
from pathlib import Path

if "-c" in sys.argv[:-1]:
    exec(sys.argv[sys.argv.index("-c") + 1])
    sys.exit(0)
multiprocessing.freeze_support()

if sys.platform != "darwin":
    raise SystemExit("This experimental package is macOS-only")
os.environ.setdefault("ND2WSI_GPU_PYRAMID", "1")
os.environ["ND2WSI_APP_NAME"] = "nd2wsi-viewer Metal Beta"
if not getattr(sys, "frozen", False):
    os.environ["ND2WSI_WINDOW_LAUNCHER"] = os.path.abspath(__file__)

import nd2wsi  # noqa: E402
from nd2wsi.metal import device_info, diagnostics, reduce2x  # noqa: E402


def beta_version():
    """The bundle's own ND2WSIPackageVersion when frozen, else an explicit override."""
    if getattr(sys, "frozen", False):
        info = Path(sys.executable).resolve().parents[1] / "Info.plist"
        return plistlib.loads(info.read_bytes()).get("ND2WSIPackageVersion")
    return os.environ.get("ND2WSI_BETA_VERSION")


BETA_VERSION = beta_version()
if BETA_VERSION:
    nd2wsi.__version__ = BETA_VERSION

if "--metal-info" in sys.argv:
    print(json.dumps({"beta": nd2wsi.__version__, "device": device_info(),
                      "diagnostics": diagnostics()}))
    raise SystemExit(0)
if "--metal-self-test" in sys.argv:
    import numpy as np

    if not device_info().get("supported"):
        raise SystemExit("Metal hardware unavailable; beta hardware test did not pass")
    for dtype in (np.uint8, np.uint16):
        a = np.random.default_rng(2026).integers(0, np.iinfo(dtype).max + 1,
                                               (3, 513, 519), dtype=dtype)
        expected = np.rint(a[:, :512, :518].reshape(3, 256, 2, 259, 2)
                           .mean(axis=(2, 4), dtype=np.float32)).astype(dtype)
        actual = reduce2x(a)
        if actual is None or not np.array_equal(actual, expected):
            raise SystemExit("Metal pixel equivalence failed")
    print(json.dumps({"ok": True, "device": device_info(), "diagnostics": diagnostics()}))
    raise SystemExit(0)

import nd2wsi.app as app  # noqa: E402

app.APP_NAME = "nd2wsi-viewer Metal Beta"
result = app.main()
if "--smoke" in sys.argv or "--gui-smoke" in sys.argv:
    print("metal beta diagnostics: " + json.dumps(diagnostics()))
raise SystemExit(result)
