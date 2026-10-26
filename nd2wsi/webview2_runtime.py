"""Choose which Microsoft Edge WebView2 runtime renders the Windows app.

A Windows build may ship Microsoft's WebView2 Fixed Version runtime in
``_internal/webview2-runtime``. Three modes, read from ``ND2WSI_WEBVIEW2_RUNTIME``:

- ``auto`` (default) prefers a valid system Evergreen runtime, which updates
  itself, and falls back to the bundled copy so a PC without WebView2 still
  opens the viewer.
- ``bundled`` forces the bundled copy. CI uses it to prove the copy renders.
- ``system`` keeps the behaviour from before bundling.

System detection follows Microsoft's documented rule: a ``pv`` value under the
EdgeUpdate client key that is present, non-empty and not ``0.0.0.0``. pywebview's
own check stops at the first unparsable value, so it can report a missing runtime
when an empty per-user value hides a valid per-machine install.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

MODE_ENV = "ND2WSI_WEBVIEW2_RUNTIME"
MODES = ("auto", "bundled", "system")
BUNDLED_DIRNAME = "webview2-runtime"
BROWSER_EXE = "msedgewebview2.exe"
CLIENT_KEY = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


@dataclass(frozen=True)
class RuntimeChoice:
    kind: str  # "system" or "bundled"
    mode: str
    path: Path | None
    system_version: str | None
    reason: str

    def report(self) -> dict:
        return {
            "kind": self.kind,
            "mode": self.mode,
            "path": None if self.path is None else str(self.path),
            "system_version": self.system_version,
            "reason": self.reason,
        }


def runtime_mode(environ=None) -> str:
    source = os.environ if environ is None else environ
    value = (source.get(MODE_ENV) or "auto").strip().lower()
    if value not in MODES:
        raise ValueError(f"{MODE_ENV} must be one of: {', '.join(MODES)}")
    return value


def required(environ=None) -> bool:
    """Whether this run must prove that the bundled copy rendered every window.

    Spelled like ``runtime_mode`` reads it, so any value that selects the bundled
    copy also requires the proof. Never raises: an invalid value already fails
    ``select_runtime`` at startup.
    """
    source = os.environ if environ is None else environ
    return (source.get(MODE_ENV) or "").strip().lower() == "bundled"


def parse_version(value) -> tuple[int, ...] | None:
    """Return the numeric version, or None for Microsoft's 'not installed' states."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(".")
    if not all(part.isdigit() for part in parts):
        return None
    numbers = tuple(int(part) for part in parts)
    return numbers if any(numbers) else None


def system_runtime_version(registry=None) -> str | None:
    """Highest valid Evergreen runtime version registered for this machine or user."""
    if registry is None:
        if sys.platform != "win32":
            return None
        try:
            import winreg as registry
        except ImportError:  # a patched platform on a non-Windows test runner
            return None
    candidates = (
        (registry.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + CLIENT_KEY),  # 64-bit Windows
        (registry.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + CLIENT_KEY),  # 32-bit Windows
        (registry.HKEY_CURRENT_USER, "Software\\" + CLIENT_KEY),
    )
    best = None
    for hive, subkey in candidates:
        try:
            with registry.OpenKey(hive, subkey) as key:
                value, _kind = registry.QueryValueEx(key, "pv")
        except OSError:
            continue
        parsed = parse_version(value)
        if parsed is not None and (best is None or parsed > best[0]):
            best = (parsed, value.strip())
    return None if best is None else best[1]


def default_app_root() -> Path:
    # pywebview resolves relative runtime paths against the same root.
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def bundled_runtime_dir(app_root=None) -> Path | None:
    folder = Path(default_app_root() if app_root is None else app_root) / BUNDLED_DIRNAME
    return folder if (folder / BROWSER_EXE).is_file() else None


# pywebview 6.2.1 (platforms/winforms.py _is_chromium) only uses a system runtime
# when .NET Framework is at least 4.6.2 and the first parsable channel `pv`, read
# HKCU before HKLM, has a major version of at least 86. An unparsable value ends
# its search. Auto mode must not pick a system runtime that pywebview rejects.
_PYWEBVIEW_CHANNEL_KEYS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # runtime
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",  # beta
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",  # dev
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",  # canary
)
_NET_462_RELEASE = 394802


def pywebview_accepts_system(registry=None) -> bool:
    """Whether pywebview's own check would render with a system WebView2 runtime."""
    if registry is None:
        if sys.platform != "win32":
            return False
        try:
            import winreg as registry
        except ImportError:
            return False

    def read(hive, subkey, name):
        with registry.OpenKey(hive, subkey) as key:
            return registry.QueryValueEx(key, name)[0]

    try:
        net = read(registry.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full", "Release")
        if not isinstance(net, int) or net < _NET_462_RELEASE:
            return False
    except OSError:
        return False
    for channel in _PYWEBVIEW_CHANNEL_KEYS:
        for hive, prefix in ((registry.HKEY_CURRENT_USER, "SOFTWARE\\"),
                             (registry.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\")):
            try:
                build = str(read(hive, prefix + "Microsoft\\EdgeUpdate\\Clients\\" + channel, "pv"))
            except OSError:
                build = "0"
            major = build.split(".")[0].strip()
            if not major.isdigit():
                return False  # pywebview's int() raises here and it gives up entirely
            if int(major) >= 86:
                return True
    return False


def choose_runtime(mode: str, *, bundled: Path | None, system_version: str | None,
                   system_usable: bool | None = None) -> RuntimeChoice:
    """Pick the runtime. ``system_usable`` is whether pywebview would accept the system one."""
    if mode not in MODES:
        raise ValueError(f"unknown WebView2 runtime mode: {mode!r}")
    if mode == "system":
        return RuntimeChoice("system", mode, None, system_version, "system runtime requested")
    if mode == "bundled":
        if bundled is None:
            raise RuntimeError("This build does not contain the bundled Microsoft Edge WebView2 runtime.")
        return _bundled(mode, bundled, system_version, "bundled runtime requested")
    usable = bool(system_version) if system_usable is None else bool(system_version) and system_usable
    if usable:
        return RuntimeChoice("system", mode, None, system_version, "valid system runtime found")
    if bundled is not None:
        reason = ("system runtime registered but pywebview would not use it; using the bundled copy"
                  if system_version else "no valid system runtime; using the bundled copy")
        return _bundled(mode, bundled, system_version, reason)
    if system_version:
        return RuntimeChoice("system", mode, None, system_version,
                             "no bundled copy; leaving the system runtime to pywebview")
    return RuntimeChoice("system", mode, None, None, "no system or bundled runtime found")


def _bundled(mode, path, system_version, reason) -> RuntimeChoice:
    if str(path).startswith("\\\\"):
        # Microsoft: a Fixed Version runtime cannot run from a network or UNC path.
        raise RuntimeError(
            "nd2wsi-viewer cannot start its bundled WebView2 runtime from a network folder. "
            "Copy the app folder to this PC and open it there."
        )
    return RuntimeChoice("bundled", mode, Path(path), system_version, reason)


def select_runtime(webview_module=None, *, environ=None, app_root=None, registry=None) -> RuntimeChoice:
    """Choose a runtime and, for the bundled copy, point pywebview at it.

    Must run before ``webview.start``: pywebview reads the setting when it imports
    its WinForms backend there.
    """
    choice = choose_runtime(
        runtime_mode(environ),
        bundled=bundled_runtime_dir(app_root),
        system_version=system_runtime_version(registry),
        system_usable=pywebview_accepts_system(registry),
    )
    if choice.kind == "bundled" and webview_module is not None:
        webview_module.settings["WEBVIEW2_RUNTIME_PATH"] = str(choice.path)
    return choice


def watch_initialization(window, record) -> bool:
    """Report how WebView2 started for ``window`` instead of a silent blank window.

    pywebview only logs a failed CoreWebView2 initialization (edgechromium.py
    on_webview_ready). ``before_show`` runs on the UI thread after the control
    exists and before its completion message is pumped, so subscribing there
    sees the result. ``record(ok, detail)`` is called on the UI thread.
    Returns False when the window offers no ``before_show`` event.
    """
    hook = getattr(getattr(window, "events", None), "before_show", None)
    if hook is None:
        return False

    def completed(_sender, args):
        try:
            if args.IsSuccess:
                record(True, None)
            else:
                record(False, str(args.InitializationException))
        except Exception as exc:  # never let a diagnostic break the UI thread
            record(False, f"could not read the WebView2 initialization result: {exc!r}")

    def attach(*_args):
        try:
            window.native.webview.CoreWebView2InitializationCompleted += completed
        except Exception as exc:
            record(False, f"could not watch WebView2 initialization: {exc!r}")

    hook += attach
    return True


# Proof of the runtime that actually rendered a window ------------------------
#
# pywebview falls back to the system runtime with only a log warning when the
# configured folder is missing, and once the setting is set its renderer is
# always "edgechromium". Neither the setting nor the version string proves which
# copy ran (the system Evergreen runtime may be the same version), so the proof
# is the image of the browser process that hosts the window.

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
EVIDENCE_KEYS = ("bundled", "browser_executable", "browser_version", "browser_process_id",
                 "requested_folder", "expected_folder", "user_data_folder", "system_version", "error")


def probe_webview2(native) -> dict:
    """Read one window's WebView2 identity. ``native`` is pywebview's BrowserForm.

    CoreWebView2 and its environment may only be touched on the WinForms UI
    thread, so call this only through a dispatcher such as
    ``translucent_smoke.native_call``, never directly from another thread.
    """
    control = native.webview
    core = control.CoreWebView2
    if core is None:
        raise RuntimeError("WebView2 is not initialized (CoreWebView2 is None)")
    environment = core.Environment
    properties = control.CreationProperties
    requested = None if properties is None else properties.BrowserExecutableFolder
    return {
        "browser_version": str(environment.BrowserVersionString),
        "browser_process_id": int(core.BrowserProcessId),
        "user_data_folder": str(environment.UserDataFolder),
        # None means pywebview did not pass a folder: the system runtime was used.
        "requested_folder": None if requested is None else str(requested),
    }


def process_image_path(pid) -> str | None:
    """Full image path of a running process, or None. Windows only; never raises."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        # A private kernel32 instance: never change prototypes other code shares.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE  # a truncated 64-bit handle is invalid
        query = kernel.QueryFullProcessImageNameW
        query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not query(handle, 0, buffer, ctypes.byref(size)):
                return None
            return buffer.value or None
        finally:
            close(handle)
    except Exception:
        return None


def _same_file(first, second) -> bool:
    if first is None or second is None:
        return False
    try:
        # samefile, not string prefixes: 8.3 short names (C:\Users\RUNNER~1),
        # case and separators all differ between APIs for the same file.
        return os.path.samefile(first, second)
    except (OSError, TypeError, ValueError):
        return False


def window_evidence(window, call, *, timeout=30, image_path=process_image_path,
                    app_root=None, registry=None) -> dict:
    """JSON-safe proof of the runtime that renders ``window``. Never raises.

    ``call(window, operation, timeout=...)`` must run ``operation(window.native)``
    on the window's UI thread and return its result (``native_call``). The
    browser process image is resolved afterwards, off the UI thread.
    ``bundled`` is True only when that image is the bundled
    ``msedgewebview2.exe`` and pywebview requested the bundled folder.
    """
    evidence = dict.fromkeys(EVIDENCE_KEYS)
    evidence["bundled"] = False
    errors = []
    expected = None
    try:
        evidence["system_version"] = system_runtime_version(registry)
    except Exception as exc:
        errors.append(f"system runtime lookup failed: {exc!r}")
    try:
        expected = bundled_runtime_dir(app_root)
        if expected is not None:
            evidence["expected_folder"] = os.path.realpath(expected)
    except Exception as exc:
        errors.append(f"bundled runtime lookup failed: {exc!r}")
    try:
        probe = call(window, probe_webview2, timeout=timeout)
        for key in ("browser_version", "browser_process_id", "user_data_folder", "requested_folder"):
            evidence[key] = probe.get(key)
    except Exception as exc:
        errors.append(f"WebView2 probe failed: {exc}")
    image = None
    if evidence["browser_process_id"] is not None:
        try:
            image = image_path(evidence["browser_process_id"])
            if image is None:
                errors.append(f"could not resolve the browser process image (pid {evidence['browser_process_id']})")
            else:
                evidence["browser_executable"] = os.path.realpath(image)
        except Exception as exc:
            errors.append(f"browser process image lookup failed: {exc!r}")
    if expected is not None:
        evidence["bundled"] = (_same_file(image, os.path.join(expected, BROWSER_EXE))
                               and _same_file(evidence["requested_folder"], expected))
    evidence["error"] = "; ".join(errors) or None
    return evidence
