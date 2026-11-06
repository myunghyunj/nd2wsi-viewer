"""Startup repair for a Windows build extracted from a downloaded ZIP.

Explorer copies a downloaded ZIP's Mark of the Web (the ``Zone.Identifier``
stream) onto every file it extracts. .NET Framework then refuses to load the
marked pythonnet and WebView2 assemblies and the viewer cannot start
("Failed to resolve Python.Runtime.Loader.Initialize"). Native DLLs and the
executable load regardless, and the executable keeps its own mark so
SmartScreen still sees it. Only the bundle's .NET assembly folders are
cleared, before anything imports ``clr``.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

from .platform_io import filesystem_path

# Every .NET assembly the app loads lives under these folders of PyInstaller's
# _internal directory; packaging/build_windows.py fails a build that adds one
# anywhere else.
ASSEMBLY_DIRS = ("pythonnet/runtime", "clr_loader/ffi/dlls", "webview/lib")
MARK_STREAM = ":Zone.Identifier"
# Filesystems without NTFS Alternate Data Streams (such as exFAT and FAT32 on
# portable SSDs) reject ":Zone.Identifier" paths with these Win32 errors.
_NO_STREAM_WINERRORS = frozenset({1, 50, 87, 123})
STARTUP_HINT = (
    "Windows may have blocked the files extracted from the downloaded ZIP. "
    "Right-click the ZIP, choose Properties, tick Unblock, then extract the whole "
    "ZIP again to a folder on this computer (not a network drive)."
)


def clear_download_marks(internal: Path, *, platform: str | None = None) -> list[str]:
    """Remove the download mark from the bundle's .NET assemblies.

    Returns one message per folder or assembly that could not be cleared (a
    read-only folder, for example). Never raises: a failure here must not stop
    a launch that may still work.
    """
    if (platform or sys.platform) != "win32":
        return []
    failures = []
    for folder in ASSEMBLY_DIRS:
        root = Path(internal, folder)
        try:
            assemblies = sorted(path for path in root.rglob("*.dll") if path.is_file())
        except OSError as exc:
            failures.append(f"{root}: {exc}")
            continue
        for assembly in assemblies:
            try:
                os.remove(filesystem_path(assembly) + MARK_STREAM)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if getattr(exc, "winerror", None) in _NO_STREAM_WINERRORS or exc.errno == errno.EINVAL:
                    continue
                failures.append(f"{assembly}: {exc}")
    return failures


def startup_hint(message: str, *, platform: str | None = None) -> str:
    """Guidance for a Windows startup failure caused by blocked assemblies."""
    if (platform or sys.platform) != "win32":
        return ""
    if "Python.Runtime" in message or "ClrLoader" in message:
        return STARTUP_HINT
    return ""
