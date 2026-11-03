"""PyInstaller entry point for the nd2wsi-viewer desktop app."""
import multiprocessing
import os
import sys
from pathlib import Path

# Libraries under the hood start multiprocessing helpers (resource_tracker)
# by re-executing this binary with python-style arguments. Without these two
# guards the helper relaunches the whole app, which shows a second window
# and leaves a zombie that later breaks Finder launches with error -600.
if "-c" in sys.argv[:-1]:
    code = sys.argv[sys.argv.index("-c") + 1]
    exec(code)
    sys.exit(0)

multiprocessing.freeze_support()

# A GUI executable may inherit legacy-encoded pipes when started by another
# application. Always use UTF-8 diagnostics, including for Korean filenames,
# and give dependencies real streams (some call .flush() without checking).
# CI sets a fresh path per smoke run; normal launches keep a per-process log.
if sys.platform == "win32":
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "nd2wsi-viewer" / "logs"
    logfile = Path(os.environ.get("ND2WSI_LOG_FILE", str(root / f"app-{os.getpid()}.log")))
    try:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        stream = open(logfile, "a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = stream
    sys.stderr = stream

# Explorer marks every file extracted from a downloaded ZIP, and .NET Framework
# then refuses pythonnet's assemblies. Clear the marks before anything imports clr.
# ND2WSI_KEEP_DOWNLOAD_MARKS=1 keeps them, to reproduce the blocked start.
if (sys.platform == "win32" and getattr(sys, "frozen", False)
        and os.environ.get("ND2WSI_KEEP_DOWNLOAD_MARKS") != "1"):
    from nd2wsi.windows_bundle import clear_download_marks

    for failure in clear_download_marks(Path(sys._MEIPASS)):
        print(f"download mark not cleared: {failure}", flush=True)

# run() ends a finished --gui-smoke at once, so no lingering thread can hold it.
from nd2wsi.app import run  # noqa: E402 - freeze support must run before app imports

if __name__ == "__main__":
    sys.exit(run())
