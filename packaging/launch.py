"""PyInstaller entry point for the nd2wsi-viewer desktop app."""
import multiprocessing
import os
import re
import sys
from pathlib import Path

# PyInstaller frozen multiprocessing support must run before any app code.
multiprocessing.freeze_support()

# CPython's multiprocessing.resource_tracker launches a child process with
# `[exe, *interpreter_flags, "-c", "from multiprocessing.resource_tracker import main;main(<fd>)"]`.
# Never execute arbitrary code passed via `-c` (that would turn the signed app
# binary into a generic Python execution proxy). Only allow that exact helper.
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


def _prune_old_logs(log_dir: Path, keep: int = 20) -> None:
    try:
        logs = sorted(
            log_dir.glob("app-*.log"),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        for stale in logs[keep:]:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


# A GUI executable may inherit legacy-encoded pipes when started by another
# application. Always use UTF-8 diagnostics, including for Korean filenames,
# and give dependencies real streams (some call .flush() without checking).
# CI sets a fresh path per smoke run; normal launches keep a per-process log.
if sys.platform == "win32":
    root = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")) / "nd2wsi-viewer" / "logs"
    explicit_log = os.environ.get("ND2WSI_LOG_FILE")
    logfile = Path(explicit_log) if explicit_log else (root / f"app-{os.getpid()}.log")
    try:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        if not explicit_log:
            _prune_old_logs(root)
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
