"""Local AstroMap beta entry point, independent of the released app/updater."""
import multiprocessing
import os
import plistlib
import re
import sys
from pathlib import Path

multiprocessing.freeze_support()

_RESOURCE_TRACKER_RE = re.compile(
    r"^from multiprocessing\.resource_tracker import main;main\((\d+)\)$"
)
if (
    len(sys.argv) >= 3
    and sys.argv[-2] == "-c"
    and all(arg.startswith("-") for arg in sys.argv[1:-2])
):
    _rt_match = _RESOURCE_TRACKER_RE.fullmatch(sys.argv[-1])
    if _rt_match is not None:
        from multiprocessing.resource_tracker import main as _resource_tracker_main

        _resource_tracker_main(int(_rt_match.group(1)))
        sys.exit(0)
    print("nd2wsi-viewer: -c execution is not supported.", file=sys.stderr)
    sys.exit(2)

if sys.platform != "darwin":
    raise SystemExit("This local Astromap beta is macOS-only")

os.environ["ND2WSI_APP_NAME"] = "nd2wsi-viewer Astromap Beta"
os.environ["ND2WSI_DISABLE_UPDATES"] = "1"
if not getattr(sys, "frozen", False):
    # A script's directory is packaging/legacy/, and an unrelated editable
    # install may otherwise win over the checkout being tested.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    os.environ["ND2WSI_WINDOW_LAUNCHER"] = str(Path(__file__).resolve())

import nd2wsi  # noqa: E402


def beta_version():
    """The bundle's own ND2WSIPackageVersion when frozen, else an explicit override."""
    if getattr(sys, "frozen", False):
        info = Path(sys.executable).resolve().parents[1] / "Info.plist"
        return plistlib.loads(info.read_bytes()).get("ND2WSIPackageVersion")
    return os.environ.get("ND2WSI_BETA_VERSION")


BETA_VERSION = beta_version()
if BETA_VERSION:
    nd2wsi.__version__ = BETA_VERSION

if __name__ == "__main__":
    # The annotation beta opens the standard viewer, where vector ROI tools
    # and exports are available. Independent windows reuse this executable.
    from nd2wsi.app import main

    raise SystemExit(main())
