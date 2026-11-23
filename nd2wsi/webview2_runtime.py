"""Choose which Microsoft Edge WebView2 runtime renders the Windows app.

A Windows build may ship Microsoft's WebView2 Fixed Version runtime in
``_internal/webview2-runtime``. Three modes, read from ``ND2WSI_WEBVIEW2_RUNTIME``:

- ``auto`` (default) prefers a valid system Evergreen runtime (major version
  ``>= 109`` with ``msedgewebview2.exe`` verified on disk), which updates
  itself, and falls back to the bundled copy so a PC without a modern WebView2
  still opens the viewer.
- ``bundled`` forces the bundled copy. CI uses it to prove the copy renders.
- ``system`` keeps the behaviour from before bundling.

System detection follows Microsoft's documented rule: a ``pv`` value under the
EdgeUpdate client key that is present, non-empty and not ``0.0.0.0``, backed by
an actual ``msedgewebview2.exe`` on disk. pywebview's own check stops at the
first unparsable value, so it can report a missing runtime when an empty
per-user value hides a valid per-machine install.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

MODE_ENV = "ND2WSI_WEBVIEW2_RUNTIME"
FALLBACK_TRIED_ENV = "ND2WSI_WEBVIEW2_FALLBACK_TRIED"
MODES = ("auto", "bundled", "system")
BUNDLED_DIRNAME = "webview2-runtime"
BROWSER_EXE = "msedgewebview2.exe"
CLIENT_KEY = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

# Minimum accepted system Evergreen WebView2 major version for auto mode.
# Required by pywebview 6.2.1 (IsInPrivateModeEnabled) and modern CSS/JS
# (:has(), Object.hasOwn()) used by the viewer shell.
MIN_SYSTEM_MAJOR = 109

# pywebview 6.2.1 (platforms/winforms.py _is_chromium) inspects these four
# EdgeUpdate channels (HKCU before HKLM) and requires .NET Framework >= 4.6.2.
_PYWEBVIEW_CHANNEL_KEYS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # runtime
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",  # beta
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",  # dev
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",  # canary
)
_CHANNEL_APP_DIRS = {
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}": ("EdgeWebView",),
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}": ("Edge Beta", "EdgeWebView"),
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}": ("Edge Dev", "EdgeWebView"),
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}": ("Edge SxS", "EdgeWebView"),
}
_NET_462_RELEASE = 394802


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


def system_version_meets_floor(version: str | None, *, min_major: int = MIN_SYSTEM_MAJOR) -> bool:
    """Whether ``version`` parses as a valid WebView2 version at or above ``min_major``."""
    parsed = parse_version(version)
    return parsed is not None and parsed[0] >= min_major


def _load_winreg(registry=None):
    if registry is not None:
        return registry
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # a patched platform on a non-Windows test runner
        return None
    return winreg


def _channel_registry_entries(registry):
    """Yield ``(channel, hive, subkey)`` across all channels in pywebview's order."""
    for channel in _PYWEBVIEW_CHANNEL_KEYS:
        suffix = "Microsoft\\EdgeUpdate\\Clients\\" + channel
        yield channel, registry.HKEY_CURRENT_USER, "SOFTWARE\\" + suffix
        yield channel, registry.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + suffix
        yield channel, registry.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + suffix


def _system_binary_candidates(
    version: str,
    *,
    hive=None,
    channel: str = _PYWEBVIEW_CHANNEL_KEYS[0],
    location: str | None = None,
    registry=None,
    environ=None,
) -> list[Path]:
    """Candidate disk locations for ``msedgewebview2.exe`` of a registered system channel."""
    candidates: list[Path] = []
    if isinstance(location, str) and location.strip():
        loc = Path(location.strip())
        candidates.append(loc / version / BROWSER_EXE)
        candidates.append(loc / BROWSER_EXE)
    source = os.environ if environ is None else environ
    hkcu = getattr(registry, "HKEY_CURRENT_USER", "HKCU")
    hklm = getattr(registry, "HKEY_LOCAL_MACHINE", "HKLM")
    roots: list[Path] = []
    if hive is None or hive == hkcu:
        local_app = source.get("LOCALAPPDATA")
        if local_app:
            roots.append(Path(local_app))
        elif environ is None:
            roots.append(Path.home() / "AppData" / "Local")
    if hive is None or hive == hklm:
        for env_key, fallback in (("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                                  ("ProgramFiles", r"C:\Program Files")):
            raw = source.get(env_key) or (fallback if environ is None else "")
            if raw:
                path = Path(raw)
                if path not in roots:
                    roots.append(path)
    app_dirs = _CHANNEL_APP_DIRS.get(channel, ("EdgeWebView",))
    for root in roots:
        for app_dir in app_dirs:
            candidates.append(root / "Microsoft" / app_dir / "Application" / version / BROWSER_EXE)
    return candidates


def system_runtime_binary_exists(
    version: str,
    *,
    hive=None,
    channel: str = _PYWEBVIEW_CHANNEL_KEYS[0],
    location: str | None = None,
    registry=None,
    environ=None,
) -> bool:
    """True when ``msedgewebview2.exe`` for ``version`` actually exists on disk."""
    for candidate in _system_binary_candidates(
        version, hive=hive, channel=channel, location=location, registry=registry, environ=environ,
    ):
        try:
            if candidate.is_file():
                return True
        except OSError:
            continue
    return False


def _read_optional_location(registry, key) -> str | None:
    try:
        value, _kind = registry.QueryValueEx(key, "location")
    except (OSError, AssertionError):
        return None
    return value if isinstance(value, str) and value.strip() else None


def system_runtime_version(
    registry=None,
    *,
    verify_binary: bool | None = None,
    binary_exists=None,
) -> str | None:
    """Highest valid Evergreen/Edge channel WebView2 runtime version for this PC or user.

    Inspects the same EdgeUpdate channels as ``pywebview_accepts_system``.
    On real Windows lookups (``registry is None``) or when ``verify_binary=True``
    / ``binary_exists`` is supplied, also verifies that ``msedgewebview2.exe``
    exists on disk so stale Windows 10 registry keys are ignored.
    """
    should_verify = (registry is None) or (binary_exists is not None) if verify_binary is None else bool(verify_binary)
    reg = _load_winreg(registry)
    if reg is None:
        return None
    checker = system_runtime_binary_exists if binary_exists is None else binary_exists
    best = None
    for channel, hive, subkey in _channel_registry_entries(reg):
        try:
            with reg.OpenKey(hive, subkey) as key:
                value, _kind = reg.QueryValueEx(key, "pv")
                location = _read_optional_location(reg, key) if should_verify else None
        except OSError:
            continue
        parsed = parse_version(value)
        if parsed is None:
            continue
        cleaned = value.strip()
        if should_verify and not checker(cleaned, hive=hive, channel=channel, location=location, registry=reg):
            continue
        if best is None or parsed > best[0]:
            best = (parsed, cleaned)
    return None if best is None else best[1]


def default_app_root() -> Path:
    # pywebview resolves relative runtime paths against the same root.
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def bundled_runtime_dir(app_root=None) -> Path | None:
    folder = Path(default_app_root() if app_root is None else app_root) / BUNDLED_DIRNAME
    return folder if (folder / BROWSER_EXE).is_file() else None


def pywebview_accepts_system(
    registry=None,
    *,
    min_major: int = MIN_SYSTEM_MAJOR,
    verify_binary: bool | None = None,
    binary_exists=None,
) -> bool:
    """Whether pywebview would render with a valid system WebView2 runtime (>= ``min_major``)."""
    should_verify = (registry is None) or (binary_exists is not None) if verify_binary is None else bool(verify_binary)
    reg = _load_winreg(registry)
    if reg is None:
        return False
    checker = system_runtime_binary_exists if binary_exists is None else binary_exists

    def read(hive, subkey, name):
        with reg.OpenKey(hive, subkey) as key:
            return reg.QueryValueEx(key, name)[0]

    try:
        net = read(reg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full", "Release")
        if not isinstance(net, int) or net < _NET_462_RELEASE:
            return False
    except OSError:
        return False
    for channel, hive, subkey in _channel_registry_entries(reg):
        location = None
        try:
            with reg.OpenKey(hive, subkey) as key:
                build = str(reg.QueryValueEx(key, "pv")[0])
                if should_verify:
                    location = _read_optional_location(reg, key)
        except OSError:
            build = "0"
        major = build.split(".")[0].strip()
        if not major.isdigit():
            return False  # pywebview's int() raises here and it gives up entirely
        if int(major) < min_major:
            continue
        if should_verify and not checker(build.strip(), hive=hive, channel=channel, location=location, registry=reg):
            continue
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
    meets_floor = system_version_meets_floor(system_version)
    usable = meets_floor if system_usable is None else (meets_floor and bool(system_usable))
    if usable:
        return RuntimeChoice("system", mode, None, system_version, "valid system runtime found")
    if bundled is not None:
        if system_version and not meets_floor:
            reason = (
                f"system runtime {system_version} is below minimum major version "
                f"{MIN_SYSTEM_MAJOR}; using the bundled copy"
            )
        elif system_version:
            reason = "system runtime registered but pywebview would not use it; using the bundled copy"
        else:
            reason = "no valid system runtime; using the bundled copy"
        return _bundled(mode, bundled, system_version, reason)
    if system_version:
        return RuntimeChoice("system", mode, None, system_version,
                             "no bundled copy; leaving the system runtime to pywebview")
    return RuntimeChoice("system", mode, None, None, "no system or bundled runtime found")


def _drive_type(root: str) -> int:
    import ctypes

    return int(ctypes.windll.kernel32.GetDriveTypeW(root))


def _is_network_path(path) -> bool:
    """A UNC path, or a drive letter Windows reports as remote (a mapped share)."""
    text = str(path)
    if text.startswith("\\\\"):
        return True
    if sys.platform != "win32" or len(text) < 2 or text[1] != ":":
        return False
    try:
        return _drive_type(text[:2] + "\\") == 4  # DRIVE_REMOTE
    except Exception:
        return False


def _bundled(mode, path, system_version, reason) -> RuntimeChoice:
    if _is_network_path(path):
        # Microsoft (Fixed Version known issues): it cannot run from a network
        # location or UNC path.
        raise RuntimeError(
            "nd2wsi-viewer cannot start its bundled WebView2 runtime from a network folder. "
            "Copy the app folder to this PC and open it there."
        )
    return RuntimeChoice("bundled", mode, Path(path), system_version, reason)


def select_runtime(
    webview_module=None,
    *,
    environ=None,
    app_root=None,
    registry=None,
    verify_binary: bool | None = None,
    binary_exists=None,
) -> RuntimeChoice:
    """Choose a runtime and, for the bundled copy, point pywebview at it.

    Must run before ``webview.start``: pywebview reads the setting when it imports
    its WinForms backend there.
    """
    choice = choose_runtime(
        runtime_mode(environ),
        bundled=bundled_runtime_dir(app_root),
        system_version=system_runtime_version(
            registry, verify_binary=verify_binary, binary_exists=binary_exists,
        ),
        system_usable=pywebview_accepts_system(
            registry, verify_binary=verify_binary, binary_exists=binary_exists,
        ),
    )
    if choice.kind == "bundled" and webview_module is not None:
        webview_module.settings["WEBVIEW2_RUNTIME_PATH"] = str(choice.path)
    return choice


def fallback_runtime_mode(
    selection: dict | None,
    *,
    environ=None,
    app_root=None,
    registry=None,
    verify_binary: bool | None = None,
    binary_exists=None,
) -> str | None:
    """Alternate runtime mode for a 1-shot ``auto`` fallback after WebView2 init failure.

    Returns ``"bundled"`` when ``system`` failed and a usable local bundled copy
    exists, ``"system"`` when ``bundled`` failed and a valid system Evergreen
    runtime exists, or ``None`` when fallback is disallowed or already tried.
    """
    if not isinstance(selection, dict) or selection.get("mode") != "auto":
        return None
    source = os.environ if environ is None else environ
    if (source.get(FALLBACK_TRIED_ENV) or "").strip() == "1":
        return None
    kind = selection.get("kind")
    if kind == "system":
        bundled = bundled_runtime_dir(app_root)
        if bundled is not None and not _is_network_path(bundled):
            return "bundled"
        return None
    if kind == "bundled":
        sys_ver = selection.get("system_version")
        if sys_ver is None and registry is None:
            return None
        if registry is not None:
            sys_ver = system_runtime_version(
                registry, verify_binary=verify_binary, binary_exists=binary_exists,
            )
        if not system_version_meets_floor(sys_ver):
            return None
        if registry is not None or verify_binary or binary_exists is not None:
            if not pywebview_accepts_system(
                registry, verify_binary=verify_binary, binary_exists=binary_exists,
            ):
                return None
        elif sys.platform == "win32" and _load_winreg(None) is not None:
            if not pywebview_accepts_system():
                return None
        return "system"
    return None


# Watch CoreWebView2 initialization (pre-before_show & synchronous-safe) -------

_EDGECHROME_HOOK_LOCK = threading.Lock()
_EDGECHROME_WATCHERS: dict[int, tuple[object, object]] = {}


def _install_edgechromium_hook() -> bool:
    """Wrap ``EdgeChrome.on_webview_ready`` once so synchronous init failures are never missed."""
    module = sys.modules.get("webview.platforms.edgechromium")
    edge_chrome = getattr(module, "EdgeChrome", None)
    if edge_chrome is None:
        return False
    with _EDGECHROME_HOOK_LOCK:
        original = getattr(edge_chrome, "on_webview_ready", None)
        if original is None:
            return False
        if getattr(original, "_nd2wsi_init_wrapped", False):
            return True

        def wrapped(self, sender, args):
            target = getattr(self, "pywebview_window", None)
            watcher = None
            if target is not None:
                with _EDGECHROME_HOOK_LOCK:
                    entry = _EDGECHROME_WATCHERS.get(id(target))
                    if entry is not None and entry[0] is target:
                        watcher = entry[1]
            if watcher is not None:
                watcher(sender, args)
            return original(self, sender, args)

        wrapped._nd2wsi_init_wrapped = True
        edge_chrome.on_webview_ready = wrapped
        return True


def watch_initialization(window, record) -> bool:
    """Report how WebView2 started for ``window`` instead of a silent blank window.

    pywebview calls ``EnsureCoreWebView2Async`` inside ``EdgeChrome.__init__``
    before ``before_show`` fires, so subscribing only in ``before_show`` can miss
    a synchronous initialization failure. This hooks ``EdgeChrome.on_webview_ready``
    during ``initialized`` (before ``BrowserForm`` is constructed) and also attaches
    directly to ``window.native.webview.CoreWebView2InitializationCompleted`` as
    soon as the control exists. ``record(ok, detail)`` is invoked at most once.
    Returns False when the window exposes neither native control nor lifecycle hooks.
    """
    events = getattr(window, "events", None)
    before_show = getattr(events, "before_show", None)
    initialized = getattr(events, "initialized", None)
    closed = getattr(events, "closed", None)

    state_lock = threading.Lock()
    state = {"attached": False, "reported": False}

    def unregister_watcher(*_args):
        with _EDGECHROME_HOOK_LOCK:
            entry = _EDGECHROME_WATCHERS.get(id(window))
            if entry is not None and entry[0] is window:
                _EDGECHROME_WATCHERS.pop(id(window), None)

    def emit(ok: bool, detail) -> None:
        with state_lock:
            if state["reported"]:
                return
            state["reported"] = True
        unregister_watcher()
        record(ok, detail)

    def completed(_sender, args):
        try:
            if args.IsSuccess:
                emit(True, None)
            else:
                emit(False, str(args.InitializationException))
        except Exception as exc:  # never let a diagnostic break the UI thread
            emit(False, f"could not read the WebView2 initialization result: {exc!r}")

    def try_attach(*_args, report_missing: bool = False) -> bool:
        with state_lock:
            if state["attached"] or state["reported"]:
                return True
        native = getattr(window, "native", None)
        webview_ctrl = getattr(native, "webview", None)
        if webview_ctrl is None:
            if report_missing:
                emit(False, "could not watch WebView2 initialization: native webview control is unavailable")
            return False
        try:
            webview_ctrl.CoreWebView2InitializationCompleted += completed
            with state_lock:
                state["attached"] = True
            if getattr(webview_ctrl, "CoreWebView2", None) is not None:
                emit(True, None)
            return True
        except Exception as exc:
            emit(False, f"could not watch WebView2 initialization: {exc!r}")
            return False

    # Register early watcher for EdgeChrome.on_webview_ready before BrowserForm exists.
    with _EDGECHROME_HOOK_LOCK:
        _EDGECHROME_WATCHERS[id(window)] = (window, completed)
    if closed is not None:
        closed += unregister_watcher

    attached_now = try_attach(report_missing=False)
    edge_hooked = _install_edgechromium_hook()

    if initialized is not None:
        def on_initialized(*_args):
            _install_edgechromium_hook()
            try_attach(report_missing=False)
            # Never return False: pywebview treats False from initialized as a veto.
            return True

        initialized += on_initialized

    if before_show is not None:
        def on_before_show(*_args):
            _install_edgechromium_hook()
            try_attach(report_missing=True)

        before_show += on_before_show

    if not attached_now and before_show is None and initialized is None and not edge_hooked:
        unregister_watcher()
        return False
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


# Windows 10 folder permission for the Fixed Version runtime -----------------
# Microsoft's WebView2 distribution docs ("The Fixed Version runtime distribution
# mode", step 6): on Windows 10, unpackaged apps using Fixed Version 120 or later
# must give the AppContainer groups read and execute access to the runtime folder,
# or its sandboxed processes cannot start. Windows 11 (build 22000 and later) does
# not need it. Grants run in-process through advapi32 (never spawning icacls.exe,
# which AppLocker / SRP policies may block on hospital and university PCs).
APPCONTAINER_SIDS = ("S-1-15-2-1", "S-1-15-2-2")  # ALL [RESTRICTED] APPLICATION PACKAGES
WINDOWS_10_LPAC_BUILD = 14393
WINDOWS_11_BUILD = 22000
GRANT_SMOKE_ENV = "ND2WSI_SMOKE_WEBVIEW2_ACL"  # CI only: run the grant where Windows does not need it
_READ_EXECUTE = 0x1200A9  # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
_GENERIC_READ = 0x80000000
_GENERIC_EXECUTE = 0x20000000
# Generic rights as Windows maps them for files: inherited grants are often
# stored with generic bits (for example GR,GE under Program Files).
_GENERIC_FILE_RIGHTS = ((0x80000000, 0x120089), (0x20000000, 0x1200A0), (0x10000000, 0x1F01FF))
_INHERITABLE = 0x3  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
_NO_INHERITANCE = 0x0
_INHERIT_ONLY = 0x8
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_GRANT_ACCESS = 1
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_WELL_KNOWN_GROUP = 5


def _current_build(build=None) -> int | None:
    if build is not None:
        return int(build)
    version = getattr(sys, "getwindowsversion", None)
    if sys.platform != "win32" or version is None:
        return None
    return int(version().build)


def required_appcontainer_sids(build=None) -> tuple[str, ...]:
    """AppContainer SIDs required on ``build`` (pre-14393 Win10 lacks LPAC ``S-1-15-2-2``)."""
    resolved = _current_build(build)
    if resolved is not None and resolved < WINDOWS_10_LPAC_BUILD:
        return (APPCONTAINER_SIDS[0],)
    return APPCONTAINER_SIDS


def needs_appcontainer_grant(build=None) -> bool:
    resolved = _current_build(build)
    return resolved is not None and resolved < WINDOWS_11_BUILD


def _file_rights(mask: int) -> int:
    for generic, specific in _GENERIC_FILE_RIGHTS:
        if mask & generic:
            mask |= specific
    return mask


_READ_EXECUTE_BITS = 0xA9  # FILE_READ_DATA | FILE_READ_EA | FILE_EXECUTE | FILE_READ_ATTRIBUTES


def appcontainer_decision(aces, groups: int = len(APPCONTAINER_SIDS), *, require_inherit: bool = True) -> str:
    """Decide from a DACL's ACEs, in order: "allowed", "missing" or "denied".

    ``aces`` holds ``(ace_type, flags, mask, group)``; ``group`` is the index of the
    matching AppContainer SID or None. An explicit deny that overlaps read or
    execute for either group wins over any allow (Windows checks access-denied
    entries first), so it is reported, never papered over by adding a grant.
    Allowed needs, per group, some allow ACE that gives read/execute on the target
    itself (not inherit-only) and, for directories (``require_inherit=True``), some
    that passes it on (OI|CI).
    """
    applies, inherits = set(), set()
    for ace_type, flags, mask, group in aces:
        if group is None:
            continue
        rights = _file_rights(mask)
        if ace_type == 1 and rights & _READ_EXECUTE_BITS:  # ACCESS_DENIED_ACE
            return "denied"
        if ace_type != 0 or rights & _READ_EXECUTE != _READ_EXECUTE:  # ACCESS_ALLOWED_ACE only
            continue
        if not flags & _INHERIT_ONLY:
            applies.add(group)
        if flags & _INHERITABLE == _INHERITABLE:
            inherits.add(group)
    everyone = set(range(groups))
    if not require_inherit:
        return "allowed" if applies == everyone else "missing"
    return "allowed" if applies == everyone and inherits == everyone else "missing"


def appcontainer_access(folder) -> bool | None:
    """Whether both AppContainer groups can read the folder and what it holds.

    None off Windows. Raises OSError when the folder's DACL cannot be read.
    """
    state = appcontainer_state(folder)
    return None if state is None else state == "allowed"


def appcontainer_state(folder, *, require_inherit: bool | None = None, build=None) -> str | None:
    """``appcontainer_decision`` for ``folder``'s DACL; None off Windows.

    Raises OSError when the path's DACL cannot be read.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    if require_inherit is None:
        try:
            require_inherit = not Path(folder).is_file()
        except OSError:
            require_inherit = True

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER(ctypes.c_void_p)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                             pointer, pointer, pointer, pointer, pointer]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, pointer]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, pointer]
    advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.EqualSid.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    class Acl(ctypes.Structure):
        _fields_ = [("revision", ctypes.c_ubyte), ("sbz1", ctypes.c_ubyte), ("size", wintypes.WORD),
                    ("count", wintypes.WORD), ("sbz2", wintypes.WORD)]

    class AceHeader(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("size", wintypes.WORD)]

    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(str(folder), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
                                          None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if status != 0:
        raise OSError(status, "Could not read the folder's permissions", str(folder))
    targets = []
    try:
        for text in required_appcontainer_sids(build):
            sid = ctypes.c_void_p()
            if not advapi.ConvertStringSidToSidW(text, ctypes.byref(sid)):
                raise ctypes.WinError(ctypes.get_last_error())
            targets.append(sid)
        if not dacl.value:
            return "allowed"  # a NULL DACL grants everyone full access
        aces = []
        for index in range(Acl.from_address(dacl.value).count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                continue
            header = AceHeader.from_address(ace.value)
            if header.type not in (0, 1):  # ACCESS_ALLOWED_ACE / ACCESS_DENIED_ACE: header, mask, SID
                continue
            mask = wintypes.DWORD.from_address(ace.value + 4).value
            group = next((position for position, target in enumerate(targets)
                          if advapi.EqualSid(ctypes.c_void_p(ace.value + 8), target)), None)
            aces.append((header.type, header.flags, mask, group))
        return appcontainer_decision(aces, len(targets), require_inherit=require_inherit)
    finally:
        for sid in targets:
            kernel.LocalFree(sid)
        if descriptor.value:
            kernel.LocalFree(descriptor)


def _grant_appcontainer_acl_win32(
    path: Path,
    *,
    sids=APPCONTAINER_SIDS,
    inherit: bool = True,
    advapi=None,
    kernel=None,
    build=None,
) -> None:
    """Grant ``GENERIC_READ | GENERIC_EXECUTE`` to AppContainer SIDs via Win32 ``advapi32``.

    In-process replacement for ``icacls.exe`` so AppLocker/SRP rules blocking
    command-line tools cannot break Windows 10 startup. Raises ``OSError`` on failure.
    """
    import ctypes
    from ctypes import wintypes

    if advapi is None or kernel is None:
        if sys.platform != "win32":
            raise OSError("Win32 ACL APIs are only available on Windows")
        pointer = ctypes.POINTER(ctypes.c_void_p)
        if advapi is None:
            advapi = ctypes.WinDLL("advapi32", use_last_error=True)
            advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, pointer]
            advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
            advapi.GetNamedSecurityInfoW.argtypes = [
                wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                pointer, pointer, pointer, pointer, pointer,
            ]
            advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
            advapi.SetEntriesInAclW.argtypes = [wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p, pointer]
            advapi.SetEntriesInAclW.restype = wintypes.DWORD
            advapi.SetNamedSecurityInfoW.argtypes = [
                wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ]
            advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
        if kernel is None:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree.restype = ctypes.c_void_p

    class TrusteeW(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", ctypes.c_void_p),
            ("MultipleTrusteeOperation", ctypes.c_int32),
            ("TrusteeForm", ctypes.c_int32),
            ("TrusteeType", ctypes.c_int32),
            ("ptstrName", ctypes.c_void_p),
        ]

    class ExplicitAccessW(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", ctypes.c_uint32),
            ("grfAccessMode", ctypes.c_int32),
            ("grfInheritance", ctypes.c_uint32),
            ("Trustee", TrusteeW),
        ]

    target_str = str(path)
    sid_ptrs: list[ctypes.c_void_p] = []
    old_dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    new_dacl = ctypes.c_void_p()
    try:
        for sid_text in sids:
            sid_ptr = ctypes.c_void_p()
            if not advapi.ConvertStringSidToSidW(sid_text, ctypes.byref(sid_ptr)):
                err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
                raise OSError(err, f"ConvertStringSidToSidW failed for {sid_text} (error {err})", target_str)
            sid_ptrs.append(sid_ptr)

        status = advapi.GetNamedSecurityInfoW(
            target_str,
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(old_dacl),
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            raise OSError(status, f"GetNamedSecurityInfoW failed (error {status})", target_str)

        inheritance = _INHERITABLE if inherit else _NO_INHERITANCE
        entries = (ExplicitAccessW * len(sid_ptrs))()
        for index, sid_ptr in enumerate(sid_ptrs):
            entry = entries[index]
            entry.grfAccessPermissions = _READ_EXECUTE
            entry.grfAccessMode = _GRANT_ACCESS
            entry.grfInheritance = inheritance
            entry.Trustee.pMultipleTrustee = None
            entry.Trustee.MultipleTrusteeOperation = 0
            entry.Trustee.TrusteeForm = _TRUSTEE_IS_SID
            entry.Trustee.TrusteeType = _TRUSTEE_IS_WELL_KNOWN_GROUP
            entry.Trustee.ptstrName = sid_ptr

        status = advapi.SetEntriesInAclW(len(sid_ptrs), entries, old_dacl, ctypes.byref(new_dacl))
        if status != 0:
            raise OSError(status, f"SetEntriesInAclW failed (error {status})", target_str)

        status = advapi.SetNamedSecurityInfoW(
            target_str,
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            new_dacl,
            None,
        )
        if status != 0:
            raise OSError(status, f"SetNamedSecurityInfoW failed (error {status})", target_str)
    except OSError:
        resolved = _current_build(build)
        if (
            tuple(sids) == APPCONTAINER_SIDS
            and resolved is not None
            and resolved < WINDOWS_10_LPAC_BUILD
        ):
            _grant_appcontainer_acl_win32(
                path,
                sids=(APPCONTAINER_SIDS[0],),
                inherit=inherit,
                advapi=advapi,
                kernel=kernel,
                build=resolved,
            )
            return
        raise
    finally:
        if new_dacl.value:
            kernel.LocalFree(new_dacl)
        if descriptor.value:
            kernel.LocalFree(descriptor)
        for sid_ptr in sid_ptrs:
            if sid_ptr.value:
                kernel.LocalFree(sid_ptr)


def ensure_appcontainer_access(folder, *, force=False, build=None, check=None, grant=None) -> str:
    """Grant the AppContainer groups read/execute on ``folder`` and ``msedgewebview2.exe``.

    Returns "not-needed", "present", "granted" or "failed: <reason>"; never raises.
    Uses in-process Win32 ``advapi32`` ACL APIs (never ``icacls.exe``). The grant on
    ``folder`` is inheritable (OI|CI) and, when ``msedgewebview2.exe`` exists inside
    ``folder``, its ACL is also verified and explicitly granted if inheritance is
    blocked. ``check`` returns an ``appcontainer_state`` value. A deny rule is
    reported and left alone.
    """
    if check is None:
        def check(target):
            return appcontainer_state(target, build=build)
    if grant is None:
        def grant(target, *, inherit: bool = True) -> None:
            _grant_appcontainer_acl_win32(target, inherit=inherit, build=build)
    if not force and not needs_appcontainer_grant(build):
        return "not-needed"
    folder_path = Path(folder)
    browser_exe = folder_path / BROWSER_EXE
    has_exe = browser_exe.is_file()

    try:
        state = check(folder)
    except Exception:  # an unreadable DACL: the grant below reports its own error
        state = None
    if state == "denied":
        return "failed: a deny rule blocks the app-sandbox groups; the folder was left unchanged"

    exe_state = None
    if has_exe:
        try:
            exe_state = check(browser_exe)
        except Exception:
            exe_state = None
        if exe_state == "denied":
            return f"failed: a deny rule blocks the app-sandbox groups on {BROWSER_EXE}; left unchanged"

    if state == "allowed" and (not has_exe or exe_state == "allowed"):
        return "present"

    if state != "allowed":
        try:
            grant(folder_path, inherit=True)
        except OSError as exc:
            return f"failed: {exc}"

    if has_exe:
        # Re-check after granting the parent folder in case inheritance applied it.
        try:
            exe_after_folder = check(browser_exe)
        except Exception:
            exe_after_folder = None
        if exe_after_folder == "denied":
            return f"failed: a deny rule blocks the app-sandbox groups on {BROWSER_EXE}; left unchanged"
        if exe_after_folder != "allowed":
            try:
                grant(browser_exe, inherit=False)
            except OSError as exc:
                return f"failed: {exc}"

    try:
        after = check(folder)
    except Exception as exc:
        return f"granted (could not re-check: {exc})"
    if after not in ("allowed", None):
        return f"failed: ACL grant succeeded but the folder is still {after}"

    if has_exe:
        try:
            exe_after = check(browser_exe)
        except Exception as exc:
            return f"granted (could not re-check {BROWSER_EXE}: {exc})"
        if exe_after not in ("allowed", None):
            return f"failed: ACL grant succeeded but {BROWSER_EXE} is still {exe_after}"

    return "granted"
