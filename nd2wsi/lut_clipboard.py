"""Small, opt-in native clipboard bridge for portable channel LUT settings.

Only a compatible LUT envelope is returned to the web view. Other clipboard
content never leaves this module. AppKit is imported lazily so browser viewers
and non-macOS builds can keep using their browser clipboard implementation.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable

FORMAT = "nd2wsi-lut/1"
MAX_TEXT_BYTES = 128 * 1024
MAX_CHANNELS = 512
MAIN_QUEUE_TIMEOUT = 2.0


def _compatible_text(text: object) -> bool:
    if not isinstance(text, str) or not text:
        return False
    try:
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            return False
        value = json.loads(text)
    except (UnicodeError, ValueError, RecursionError):
        return False
    return (
        isinstance(value, dict)
        and value.get("format") == FORMAT
        and isinstance(value.get("channels"), list)
        and len(value["channels"]) <= MAX_CHANNELS
    )


def _on_main_queue(operation: Callable[[], dict]) -> dict:
    """Wait briefly for Cocoa, skipping callbacks still queued after timeout."""
    from Foundation import NSOperationQueue, NSThread

    if NSThread.isMainThread():
        return operation()

    finished = threading.Event()
    cancelled = threading.Event()
    result = {}

    def run():
        try:
            if not cancelled.is_set():
                result["value"] = operation()
        except Exception:
            result["value"] = {
                "ok": False, "message": "Could not access the system clipboard."
            }
        finally:
            finished.set()

    NSOperationQueue.mainQueue().addOperationWithBlock_(run)
    if not finished.wait(MAIN_QUEUE_TIMEOUT):
        cancelled.set()
        return {"ok": False, "message": "The system clipboard did not respond in time."}
    return result["value"]


def read_lut_clipboard() -> dict:
    """Read LUT text only; do not expose unrelated clipboard contents."""
    if sys.platform != "darwin":
        return {"ok": False, "message": "Native LUT clipboard is unsupported on this platform."}

    def read():
        import AppKit

        text = AppKit.NSPasteboard.generalPasteboard().stringForType_(
            AppKit.NSPasteboardTypeString
        )
        if not _compatible_text(text):
            return {"ok": False, "message": "Clipboard does not contain compatible LUT settings."}
        return {"ok": True, "text": text}

    try:
        return _on_main_queue(read)
    except Exception:
        return {"ok": False, "message": "Could not access the system clipboard."}


def write_lut_clipboard(text: str) -> dict:
    """Replace clipboard text only after validating the LUT format envelope."""
    if not _compatible_text(text):
        return {"ok": False, "message": "Copy needs compatible LUT settings."}
    if sys.platform != "darwin":
        return {"ok": False, "message": "Native LUT clipboard is unsupported on this platform."}

    def write():
        import AppKit

        clipboard = AppKit.NSPasteboard.generalPasteboard()
        clipboard.clearContents()
        if not clipboard.setString_forType_(text, AppKit.NSPasteboardTypeString):
            return {"ok": False, "message": "Could not copy LUT settings to the clipboard."}
        return {"ok": True}

    try:
        return _on_main_queue(write)
    except Exception:
        return {"ok": False, "message": "Could not access the system clipboard."}
