"""Passively observe the W/A/S/D keys SLACS sends to the ACS stage.

The user jogs the sample stage from the legacy SLACS program with W/A/S/D. This
module watches those keystrokes and moves the translucent overlay to match, using
the same commanded-displacement path as the HTTP receiver. W/A/S/D move the
overlay in the directions shown on screen: up, left, down and right, whatever
rotation or flip the main viewer handed to the overlay (orientation is matched in
the main viewer before the overlay opens). Only these key presses are followed;
mouse clicks and other stage moves are never observed. It is a strict
observer: on Windows it uses Raw Input (``RIDEV_INPUTSINK``), which never consumes,
delays, alters, or blocks a keystroke, and it never calls into SLACS or the ACS
controller. SLACS's own keystrokes and its commands to the stage are untouched.

SLACS jogs the stage by its Roughness slider (µm per press), and its Mirror
Horizontal / Mirror Vertical checkboxes reverse W/S and A/D. With
``step_source="slacs"`` each press moves the overlay by the Roughness a
read-only controls reader last saw and applies the Mirror checkboxes the same
way; while Roughness cannot be read, the manual step is used. The user's two
Follow flips (``flip_x`` for A/D, ``flip_y`` for W/S) reverse the same screen
axes once more, from the next press on; they never change the image. W/A/S/D
key-ups are observed too, so a held, autorepeating jog key shows in the session
log.

SLACS's ``,`` (laser shot) and ``.`` (next well, the 96-well collection stage)
never jog the sample stage, so they never move the overlay and are never
counted. While a follower runs, each physical press of one made in SLACS is
logged once as ``slacs_key`` with the overlay's context, its image centre
included; an autorepeating hold adds one ``slacs_key_hold`` at its key-up. Each
following overlay logs its own record of a press; the records share a ``press``
number.

Privacy: Raw Input delivers every keystroke to this process. Anything that is not
one of the four jog keys or SLACS's ``,`` and ``.``, pressed or released, is
discarded the instant it arrives; it is never stored, counted individually, or
written to the session log. Ctrl/Alt/Win only set a held flag, so a jog key
pressed with them can be ignored. W/A/S/D pressed while SLACS is not the
foreground program are only tallied as ``ignored_not_slacs``, never recorded
individually; ``,`` and ``.`` pressed there, or with a Windows key held, are
not even tallied. A held key is logged only at a press or key-up made while
SLACS is in front and is timed by the presses SLACS got, so a key released
elsewhere is never recorded.

The native Raw Input plumbing lives in :mod:`nd2wsi.stage_keys_win32` and is
Windows-only, but this module imports cleanly on every platform: all Win32 use
is deferred into functions that run only on Windows or when a test injects its
own observer, foreground provider, and identifier. One process-wide Raw Input
registration is shared by every follower.
"""

from __future__ import annotations

import math
import sys
import threading
import time
from collections.abc import Callable

from . import session_log
from .slacs_identity import SCRIPT_NAME as SCRIPT_NAME
from .slacs_identity import command_line_is_slacs as command_line_is_slacs
from .stage_keys_win32 import _GENERIC_MODIFIERS as _GENERIC_MODIFIERS
from .stage_keys_win32 import _GET_RAW_INPUT_ERROR as _GET_RAW_INPUT_ERROR
from .stage_keys_win32 import _HWND_MESSAGE as _HWND_MESSAGE
from .stage_keys_win32 import (
    _PROCESS_COMMAND_LINE_INFORMATION as _PROCESS_COMMAND_LINE_INFORMATION,
)
from .stage_keys_win32 import (
    _PROCESS_QUERY_LIMITED_INFORMATION as _PROCESS_QUERY_LIMITED_INFORMATION,
)
from .stage_keys_win32 import _RAW_INPUT_HUB as _RAW_INPUT_HUB
from .stage_keys_win32 import _RI_KEY_BREAK as _RI_KEY_BREAK
from .stage_keys_win32 import _RI_KEY_E0 as _RI_KEY_E0
from .stage_keys_win32 import _RID_INPUT as _RID_INPUT
from .stage_keys_win32 import _RIDEV_INPUTSINK as _RIDEV_INPUTSINK
from .stage_keys_win32 import _RIDEV_REMOVE as _RIDEV_REMOVE
from .stage_keys_win32 import _RIM_TYPEKEYBOARD as _RIM_TYPEKEYBOARD
from .stage_keys_win32 import _SIDE_VKS as _SIDE_VKS
from .stage_keys_win32 import _SIDED_MODIFIERS as _SIDED_MODIFIERS
from .stage_keys_win32 import _WM_DESTROY as _WM_DESTROY
from .stage_keys_win32 import _WM_INPUT as _WM_INPUT
from .stage_keys_win32 import _WM_QUIT as _WM_QUIT
from .stage_keys_win32 import _modifier_side as _modifier_side
from .stage_keys_win32 import _ModifierTracker as _ModifierTracker
from .stage_keys_win32 import _RawInputHub as _RawInputHub
from .stage_keys_win32 import _RawInputKeyObserver as _RawInputKeyObserver
from .stage_keys_win32 import _SharedKeyObserver as _SharedKeyObserver
from .stage_keys_win32 import _win32 as _win32
from .stage_keys_win32 import _win_error as _win_error
from .stage_keys_win32 import _windows_foreground_process as _windows_foreground_process
from .stage_keys_win32 import _windows_process_command_line as _windows_process_command_line
from .stage_keys_win32 import _windows_process_image_name as _windows_process_image_name
from .stage_keys_win32 import _windows_process_is_slacs as _windows_process_is_slacs
from .stage_keys_win32 import _windows_side_down as _windows_side_down

# SLACS's exact jog bindings are still being confirmed by another team, so the
# whole policy lives in one place that is easy to change: which virtual key is
# which jog letter, and the unit screen direction it moves the overlay's view
# (scaled by step_um). Directions are screen right (+x) and screen down (+y) as
# displayed, the receiver's commanded axes: W moves the view up, S down, A left
# and D right. Only these keys move the overlay; Mirror and the user's Follow
# flips may reverse an axis.
KEY_BINDINGS: dict[int, tuple[str, tuple[float, float]]] = {
    0x57: ("W", (0.0, -1.0)),
    0x53: ("S", (0.0, 1.0)),
    0x41: ("A", (-1.0, 0.0)),
    0x44: ("D", (1.0, 0.0)),
}
# The one value to change if, on the instrument, the overlay moves opposite to
# SLACS's live image: -1 reverses all four keys at once. Not a user setting;
# the user's per-axis Follow flips compose with it.
KEY_VIEW_SIGN = 1
# SLACS keys that act without jogging the sample stage (SLACS.py onKeyPress): logged, never followed.
MARK_KEYS: dict[int, tuple[str, str]] = {
    0xBC: (",", "laser_shot"),   # VK_OEM_COMMA = SLACS key 44
    0xBE: (".", "next_well"),    # VK_OEM_PERIOD = SLACS key 46 (96-well stage, axes 4/5)
}
# A jog key held together with any of these is a shortcut, not a stage jog.
BLOCKING_MODIFIERS = frozenset(("ctrl", "alt", "win"))
MAX_STEP_UM = 10000.0
# "slacs": SLACS's Roughness per press (manual step while unreadable); "manual": step_um.
STEP_SOURCES = ("slacs", "manual")
# A jog key pressed again this long after its last key-down cannot be autorepeat
# (Windows repeats after at most 1 s, then at least 2.5 times a second): its
# key-up was lost, for example to the secure desktop, and a new press begins.
HOLD_RESET_SECONDS = 2.0

# Receiver rejections that mean following can no longer continue (paused/closed).
_TERMINAL_STATUSES = frozenset((409, 410))


def _keyboard_raw_input_device(api, flags: int, hwnd):
    """Build the single Raw Input descriptor for the generic-desktop keyboard."""
    return api.RAWINPUTDEVICE(0x01, 0x06, flags, hwnd)


def _validate_step_um(step_um) -> float:
    if isinstance(step_um, bool) or not isinstance(step_um, (int, float)):
        raise ValueError("step_um must be a number")
    value = float(step_um)
    if not math.isfinite(value) or not 0 < value <= MAX_STEP_UM:
        raise ValueError(f"step_um must be a finite number in (0, {MAX_STEP_UM:g}]")
    return value


def _usable_step(value) -> float | None:
    """``value`` as a step in µm when it is a valid one, else None."""
    try:
        return _validate_step_um(value)
    except (ValueError, OverflowError):
        return None


def _validate_pixel_size_um(pixel_size_um) -> tuple[float, float] | None:
    if pixel_size_um is None:
        return None
    if (not isinstance(pixel_size_um, (list, tuple)) or len(pixel_size_um) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in pixel_size_um)):
        raise ValueError("pixel_size_um must be (x_um, y_um)")
    x_um, y_um = (float(v) for v in pixel_size_um)
    if not all(math.isfinite(v) and v > 0 for v in (x_um, y_um)):
        raise ValueError("pixel_size_um must be two finite positive numbers")
    return x_um, y_um


def _validate_flip(value, name) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false")
    return value


def _modifier_names(modifiers) -> set[str]:
    """The held modifier names, lowercased, from a tuple of names or a {name: held} dict."""
    if not modifiers:
        return set()
    if isinstance(modifiers, dict):
        held = {name for name, value in modifiers.items() if value}
    else:
        held = set(modifiers)
    return {str(name).lower() for name in held}


def _flag(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _rounded_ms(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(round(value))


# What the follower knows of SLACS's controls without a reader.
_NO_READING = {"roughness_um": None, "mirror_h": None, "mirror_v": None,
               "controls_ok": None, "slacs_busy": None, "reading_age_ms": None}


class _KeyHold:
    """One jog key held down: its last key-down and the presses counted since.

    Only counted presses, all made in SLACS, time the hold; its first key-down,
    later repeats and key-up may reach another program.
    """

    __slots__ = ("last_down", "first_counted", "last_counted", "presses", "repeats")

    def __init__(self, now: float):
        self.last_down = now
        self.first_counted: float | None = None  # clock time of the first counted press
        self.last_counted: float | None = None
        self.presses = 0  # counted presses during this hold, the first one included
        self.repeats = 0  # counted autorepeat presses during this hold


class _MarkHold:
    """One ``,`` or ``.`` held down: its first key-down, made in SLACS, and its repeats there."""

    __slots__ = ("first_down", "last_down", "last_repeat", "repeats", "press")

    def __init__(self, now: float):
        self.first_down = now
        self.last_down = now
        self.last_repeat = now  # the last autorepeat SLACS got; times the hold
        self.repeats = 0  # autorepeat key-downs made while SLACS was in front
        self.press: int | None = None  # the logged press's number in this process


class SlacsKeyFollower:
    """Count SLACS W/A/S/D key-down events and drive the overlay receiver.

    SLACS's ``,`` and ``.`` presses are only logged, apart from the jog path.
    Dependencies are injected so the logic is fully testable off Windows:

    - ``observer``: an object with ``start(on_event) -> bool`` and ``stop()``.
      ``on_event(vk, down, modifiers)`` is called for every keystroke the observer
      sees. When omitted, a real Raw Input observer is created on Windows and the
      follower reports ``supported=False`` elsewhere.
    - ``foreground_provider() -> dict | None`` returns ``{"pid", "created"}`` for
      the process owning the foreground window, or None on any error.
    - ``identifier(pid, created) -> bool`` says whether that process is SLACS.
    - ``clock`` times key holds and the gap between presses.
      ``heartbeat_interval`` drives the idle heartbeat; pass
      ``heartbeat_interval=None`` to drive ``_heartbeat_tick`` manually in tests.
    - ``step_source``: "manual" moves the overlay ``step_um`` per press.
      "slacs" moves it by SLACS's Roughness as ``controls`` last read it and
      applies SLACS's Mirror checkboxes; while Roughness cannot be read,
      ``step_um`` is the fallback.
    - ``controls``: an object with ``start() -> bool``, ``stop()`` and
      ``latest() -> dict``, shaped like ``slacs_controls.SlacsControlsMonitor``.
      Used only with ``step_source="slacs"``; the follower starts and stops it.
    - ``pixel_size_um``: the overlay's ``(x_um, y_um)`` pixel size, only so each
      logged press also shows its displacement in pixels.
    - ``flip_x`` / ``flip_y``: the user's Follow flips, reversing the on-screen
      move of A/D or W/S; ``set_flips`` changes them from the next press on.
    - ``mark_context() -> dict``: the overlay's state for each logged SLACS
      ``,``/``.`` press (image centre, Follow progress, SLACS session). It runs
      on the observer thread, so it must never wait for the overlay's lock.
    """

    def __init__(self, receiver, session_id: str, *, step_um,
                 observer=None, foreground_provider: Callable[[], dict | None] | None = None,
                 identifier: Callable[[int, object], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 heartbeat_interval: float | None = 1.0, platform: str | None = None,
                 step_source: str = "manual", controls=None,
                 pixel_size_um: tuple[float, float] | None = None,
                 flip_x: bool = False, flip_y: bool = False,
                 mark_context: Callable[[], dict] | None = None,
                 on_mark: Callable[[dict], None] | None = None):
        if step_source not in STEP_SOURCES:
            raise ValueError("step_source must be 'slacs' or 'manual'")
        self._receiver = receiver
        self._session_id = session_id
        self._step_um = _validate_step_um(step_um)
        self._step_source = step_source
        # A manual step never consults SLACS, so its reader is not even started.
        self._controls = controls if step_source == "slacs" else None
        self._pixel_size_um = _validate_pixel_size_um(pixel_size_um)
        self._flip_x = _validate_flip(flip_x, "flip_x")
        self._flip_y = _validate_flip(flip_y, "flip_y")
        self._mark_context = mark_context
        self._mark_callback = on_mark
        self._clock = clock
        self._heartbeat_interval = heartbeat_interval
        self._lock = threading.Lock()
        self._started = False
        self._running = False
        self._stopped = False
        self._counted = 0
        self._ignored_not_slacs = 0
        self._last_key: str | None = None
        self._step_um_last: float | None = None
        self._last_counted_at: float | None = None
        self._held: dict[str, _KeyHold] = {}  # jog letter -> its key-down not yet released
        self._mark_held: dict[str, _MarkHold] = {}  # "," or "." -> its key-down not yet released
        self._slacs_in_front = False
        self._error: str | None = None
        self._seq = 0
        self._identity_cache: dict[tuple, bool] = {}
        self._last_foreground: tuple | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()

        resolved_platform = sys.platform if platform is None else platform
        if observer is not None:
            self._observer = observer
            self._supported = True
        elif resolved_platform == "win32":
            # A subscription to the process's single Raw Input sink, so several
            # overlays can follow at once without stealing each other's input.
            self._observer = _SharedKeyObserver()
            self._supported = True
        else:
            self._observer = None
            self._supported = False
        self._foreground_provider = foreground_provider
        self._identifier = identifier
        if self._supported and observer is None and resolved_platform == "win32":
            # Default to the real, read-only Windows providers.
            if self._foreground_provider is None:
                self._foreground_provider = _windows_foreground_process
            if self._identifier is None:
                self._identifier = _windows_process_is_slacs

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> dict:
        with self._lock:
            idle = self._started or self._stopped
            if not idle:
                self._started = True
                self._running = self._supported
                if not self._supported:
                    self._error = "Key following is only available on Windows."
        if idle:
            return self.snapshot()
        if not self._supported:
            session_log.event("follow_keys_start", step_um=self._step_um,
                              step_source=self._step_source, supported=False)
            return self.snapshot()
        # Before the observer, so a first reading is likely in by the first press.
        controls_started = self._start_controls()
        started = False
        try:
            started = bool(self._observer.start(self._on_event))
        except Exception as exc:  # noqa: BLE001 - never let a driver failure escape
            self._fail(f"Could not start the keystroke observer: {exc}")
        if not started:
            reason = self._error or getattr(self._observer, "error", None)
            if reason and not reason.startswith("Could not start"):
                reason = f"Could not start the keystroke observer: {reason}"
            self._fail(reason or "Could not start the keystroke observer.", stop_counting=True)
            try:
                self._observer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._stop_controls()
            session_log.event("follow_keys_start", step_um=self._step_um,
                              step_source=self._step_source, ok=False, error=self._error)
            return self.snapshot()
        if self._heartbeat_interval:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, name="nd2wsi-stage-keys-heartbeat", daemon=True)
            self._heartbeat_thread.start()
        session_log.event("follow_keys_start", step_um=self._step_um, step_source=self._step_source,
                          ok=True, controls_started=controls_started)
        return self.snapshot()

    def stop(self, reason: str = "stopped") -> None:
        """Stop observing. Idempotent, safe from any thread, and never hangs."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            was_active = self._running or self._started
            self._running = False
            self._held.clear()
            self._mark_held.clear()
            heartbeat = self._heartbeat_thread
            counted, ignored = self._counted, self._ignored_not_slacs
        self._heartbeat_stop.set()
        observer = self._observer
        if observer is not None:
            try:
                observer.stop()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        if heartbeat is not None and heartbeat is not threading.current_thread():
            heartbeat.join(timeout=2.0)
        self._stop_controls()
        if was_active and self._supported:
            session_log.event("follow_keys_stop", step_um=self._step_um,
                              step_source=self._step_source, reason=reason,
                              counted=counted, ignored_not_slacs=ignored)

    def _start_controls(self) -> bool | None:
        """Start the SLACS controls reader this follower owns; never raise.

        None when there is no reader (a manual step). A reader that fails to
        start leaves every press on the manual step.
        """
        controls = self._controls
        if controls is None:
            return None
        try:
            started = bool(controls.start())
        except Exception as exc:  # noqa: BLE001 - Follow still works on the manual step
            started = False
            session_log.event("follow_keys_controls_error",
                              message=f"Could not start the SLACS controls reader: {exc}")
        with self._lock:
            stopped = self._stopped
        if stopped:
            self._stop_controls()  # stop() ran meanwhile; never leave the reader polling
        return started

    def _stop_controls(self) -> None:
        controls = self._controls
        if controls is None:
            return
        try:
            controls.stop()
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass

    # -- direction flips ---------------------------------------------------

    def set_flips(self, flip_x, flip_y) -> tuple[bool, bool]:
        """Reverse the on-screen A/D (flip_x) or W/S (flip_y) move from the next press on."""
        flip_x, flip_y = _validate_flip(flip_x, "flip_x"), _validate_flip(flip_y, "flip_y")
        with self._lock:
            previous = (self._flip_x, self._flip_y)
            self._flip_x, self._flip_y = flip_x, flip_y
        return previous

    # -- key handling ------------------------------------------------------

    def _on_event(self, vk, down, modifiers=()) -> None:
        """Handle one raw keystroke. Keys other than W/A/S/D, "," and "." are dropped immediately."""
        if vk in MARK_KEYS:
            self._on_mark(vk, down, modifiers)  # logged only: it never touches the jog state
            return
        binding = KEY_BINDINGS.get(vk)
        if binding is None:
            return  # privacy: every other key is discarded the instant it arrives
        letter, (unit_x, unit_y) = binding
        if not down:
            self._key_up(letter)  # a key-up only ends a hold; it never moves the overlay
            return
        now = self._clock()
        repeat, lost = self._key_down(letter, now)
        if self._has_blocking_modifier(modifiers):
            return  # a jog key with Ctrl/Alt/Win is a shortcut, not a stage jog
        with self._lock:
            if not self._running:
                return
            manual_step = self._step_um
            flip_x, flip_y = self._flip_x, self._flip_y
        if not self._check_foreground():
            with self._lock:
                self._ignored_not_slacs += 1
            return  # counted only in aggregate; the individual key is never recorded
        if lost is not None:
            # Logged at this press because SLACS got it; a lost hold is dropped
            # when its key is next pressed with a modifier or in another program.
            self._log_hold(letter, lost, key_up=False)
        reading = self._controls_reading()
        step, step_from = manual_step, "manual"
        if self._step_source == "slacs":
            if reading["roughness_um"] is not None:
                step, step_from = reading["roughness_um"], "slacs"
            else:
                step_from = "fallback"
        # SLACS's Mirror Vertical reverses A/D (stage axis 0) and Mirror Horizontal
        # reverses W/S (stage axis 1); the overlay reverses the same screen axis.
        # The user's flips reverse it once more: each reversal flips the sign.
        x_sign = (-1 if reading["mirror_v"] else 1) * (-1 if flip_x else 1)
        y_sign = (-1 if reading["mirror_h"] else 1) * (-1 if flip_y else 1)
        # + 0.0 turns -0.0 into 0.0 for the receiver and the log.
        delta = [KEY_VIEW_SIGN * x_sign * unit_x * step + 0.0,
                 KEY_VIEW_SIGN * y_sign * unit_y * step + 0.0]
        with self._lock:
            if not self._running:
                return
            seq = self._seq + 1
        result = self._receiver.submit({
            "version": 1, "session_id": self._session_id, "seq": seq,
            "type": "delta", "delta_um": delta})
        if result.get("ok"):
            with self._lock:
                self._seq = seq
                self._counted += 1
                self._last_key = letter
                self._step_um_last = step
                previous, self._last_counted_at = self._last_counted_at, now
                hold = self._held.get(letter)
                if hold is not None:
                    if hold.first_counted is None:
                        hold.first_counted = now
                    hold.last_counted = now
                    hold.presses += 1
                    if repeat:
                        hold.repeats += 1
            gap_ms = None if previous is None else int(round((now - previous) * 1000))
            session_log.event("follow_key", key=letter, seq=seq, delta_um=delta, step_um=step,
                              step_from=step_from, roughness_um=reading["roughness_um"],
                              mirror_h=reading["mirror_h"], mirror_v=reading["mirror_v"],
                              reading_age_ms=reading["reading_age_ms"],
                              slacs_busy=reading["slacs_busy"], repeat=repeat, gap_ms=gap_ms,
                              delta_px=self._delta_px(delta), flip_x=flip_x, flip_y=flip_y)
        else:
            self._handle_submit_failure(result)

    def _key_down(self, letter: str, now: float) -> tuple[bool, _KeyHold | None]:
        """Record a jog key-down; return (is autorepeat, a hold whose key-up was lost)."""
        with self._lock:
            hold = self._held.get(letter)
            if hold is not None and now - hold.last_down <= HOLD_RESET_SECONDS:
                hold.last_down = now  # down again without an up: the key autorepeats
                return True, None
            self._held[letter] = _KeyHold(now)
            return False, hold

    def _key_up(self, letter: str) -> None:
        with self._lock:
            hold = self._held.pop(letter, None)
        # The record is stamped at the release, so it is written only while SLACS
        # still has the foreground: a jog key released elsewhere is never recorded.
        if hold is not None and hold.repeats and self._foreground_identity()[1]:
            self._log_hold(letter, hold, key_up=True)

    def _log_hold(self, letter: str, hold: _KeyHold, *, key_up: bool) -> None:
        """Log a hold that autorepeated into counted presses; taps are not logged.

        ``seconds`` runs from the hold's first counted press to its last, so
        neither end is a keystroke another program got.
        """
        if hold.repeats < 1:
            return  # also keeps holds outside SLACS out of the log: none of them counted
        with self._lock:
            if not self._running:
                return
        session_log.event("follow_key_hold", key=letter, presses=hold.presses,
                          seconds=round(hold.last_counted - hold.first_counted, 3), key_up=key_up)

    # -- SLACS laser shot and next well --------------------------------------

    def _on_mark(self, vk, down, modifiers=()) -> None:
        """Log one physical SLACS "," (laser shot) or "." (next well) press; never raises.

        Apart from the jog path: a mark never moves the overlay, is never counted
        or submitted, and never reads the jog holds or SLACS's controls. Each new
        press made with SLACS in front writes one ``slacs_key`` with the
        overlay's context; its autorepeats only add to a ``slacs_key_hold``
        written at the key-up. Overlays following at once each log the same
        press, with the same ``press`` number. The foreground is only asked, so
        a mark leaves no ``slacs_foreground`` record of its own. Presses in
        another program, and presses with a Windows key held (the shell takes
        those, SLACS never sees them), leave no trace.
        """
        key, action = MARK_KEYS[vk]
        if not down:
            with self._lock:
                hold = self._mark_held.pop(key, None) if self._running else None
            # Stamped at the release, so written only while SLACS still has the foreground.
            if hold is not None and hold.repeats and self._foreground_identity()[1]:
                self._log_mark_hold(key, action, hold, key_up=True)
            return
        now = self._clock()
        held = _modifier_names(modifiers)
        if "win" in held:
            return
        in_front = self._foreground_identity()[1]
        with self._lock:
            if not self._running:
                return
            hold = self._mark_held.get(key)
            if hold is not None and now - hold.last_down <= HOLD_RESET_SECONDS:
                hold.last_down = now  # down again without an up: the key autorepeats
                if in_front:
                    hold.repeats += 1
                    hold.last_repeat = now
                return
            if not in_front:
                self._mark_held.pop(key, None)
                return  # another program's keystroke: neither recorded nor tallied
            pressed = self._mark_held[key] = _MarkHold(now)
        if hold is not None and hold.repeats:
            self._log_mark_hold(key, action, hold, key_up=False)  # its key-up was lost
        # Key events arrive one at a time on the observer thread: none reads the hold before this.
        pressed.press = self._press_number()
        fields = {"key": key, "action": action, "press": pressed.press, "modifiers": sorted(held)}
        for name, value in self._mark_fields().items():
            fields.setdefault(name, value)
        session_log.event("slacs_key", **fields)
        if action == "laser_shot" and self._mark_callback is not None:
            try:
                self._mark_callback({**fields, "receiver_id": self._session_id})
            except Exception:  # noqa: BLE001 - recording must never affect a key observer
                pass

    def _log_mark_hold(self, key: str, action: str, hold: _MarkHold, *, key_up: bool) -> None:
        session_log.event("slacs_key_hold", key=key, action=action, press=hold.press, repeats=hold.repeats,
                          seconds=round(hold.last_repeat - hold.first_down, 3), key_up=key_up)

    def _press_number(self) -> int | None:
        """This press's number from the shared Raw Input hub, or None without one."""
        numbering = getattr(self._observer, "press_number", None)
        if not callable(numbering):
            return None
        try:
            number = numbering()
        except Exception:  # noqa: BLE001 - the press is still logged
            return None
        return number if type(number) is int else None

    def _mark_fields(self) -> dict:
        """The overlay's context for a mark record; a failure is named, never raised."""
        provider = self._mark_context
        if provider is None:
            return {"context_error": "unavailable"}
        try:
            context = provider()
        except Exception as exc:  # noqa: BLE001 - the press is still logged
            return {"context_error": type(exc).__name__}
        return context if isinstance(context, dict) else {"context_error": "unavailable"}

    def _delta_px(self, delta) -> list[float] | None:
        if self._pixel_size_um is None:
            return None
        x_um, y_um = self._pixel_size_um
        return [round(delta[0] / x_um, 2) + 0.0, round(delta[1] / y_um, 2) + 0.0]

    def _controls_reading(self) -> dict:
        """SLACS's Roughness and Mirror as the controls reader last saw them.

        Every field is None without a reader. A reader that fails or answers
        something unusable counts as unreadable: the press then falls back to
        the manual step and is not mirrored. ``roughness_um`` is only ever a
        step this follower would use.
        """
        reading = dict(_NO_READING)
        controls = self._controls
        if controls is None:
            return reading
        try:
            latest = controls.latest()
        except Exception:  # noqa: BLE001 - an unreadable SLACS never fails a press
            latest = None
        if not isinstance(latest, dict):
            reading["controls_ok"] = False
            return reading
        reading.update(
            roughness_um=_usable_step(latest.get("roughness_um")),
            mirror_h=_flag(latest.get("mirror_h")), mirror_v=_flag(latest.get("mirror_v")),
            controls_ok=_flag(latest.get("ok")), slacs_busy=_flag(latest.get("busy")),
            reading_age_ms=_rounded_ms(latest.get("age_ms")))
        return reading

    @staticmethod
    def _has_blocking_modifier(modifiers) -> bool:
        return bool(BLOCKING_MODIFIERS & _modifier_names(modifiers))

    # -- foreground identification ----------------------------------------

    def _foreground_identity(self) -> tuple[object, bool]:
        """(pid, whether it is SLACS) for the foreground window; fail closed.

        Only asks: unlike ``_check_foreground`` it neither updates the follower's
        foreground state nor logs a change, so a key-up leaves no record of its own.
        """
        provider = self._foreground_provider
        foreground = None
        if provider is not None:
            try:
                foreground = provider()
            except Exception:  # noqa: BLE001 - any failure means "not SLACS"
                foreground = None
        if not isinstance(foreground, dict):
            return None, False
        pid = foreground.get("pid")
        return pid, self._identify(pid, foreground.get("created"))

    def _check_foreground(self) -> bool:
        """Return whether SLACS owns the foreground window; fail closed."""
        pid, identified = self._foreground_identity()
        with self._lock:
            self._slacs_in_front = identified
            marker = (pid, identified)
            changed = marker != self._last_foreground
            self._last_foreground = marker
        if changed:
            session_log.event("slacs_foreground", pid=pid, identified=identified)
        return identified

    def _identify(self, pid, created) -> bool:
        if pid is None or self._identifier is None:
            return False
        cache_key = (pid, created)
        with self._lock:
            if cache_key in self._identity_cache:
                return self._identity_cache[cache_key]
        try:
            result = bool(self._identifier(pid, created))
        except Exception:  # noqa: BLE001 - fail closed on any identification error
            result = False
        with self._lock:
            if len(self._identity_cache) > 256:
                self._identity_cache.clear()
            self._identity_cache[cache_key] = result
        return result

    # -- heartbeat ---------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        interval = self._heartbeat_interval or 1.0
        while not self._heartbeat_stop.wait(interval):
            self._heartbeat_tick()

    def _heartbeat_tick(self) -> None:
        """Keep the receiver fresh and the foreground status current while idle."""
        with self._lock:
            if not self._running:
                return
        self._check_foreground()
        with self._lock:
            if not self._running:
                return
        result = self._receiver.submit({"version": 1, "session_id": self._session_id})
        if not result.get("ok"):
            self._handle_submit_failure(result)

    # -- failure handling --------------------------------------------------

    def _handle_submit_failure(self, result: dict) -> None:
        status = result.get("status")
        message = result.get("message") or "Stage receiver rejected an observation."
        if status in _TERMINAL_STATUSES:
            self._fail(message, stop_counting=True)

    def _fail(self, message: str, *, stop_counting: bool = False) -> None:
        with self._lock:
            if stop_counting:
                self._running = False
            already = self._error == message
            self._error = message
        if not already:
            session_log.event("follow_keys_error", message=message)

    # -- introspection -----------------------------------------------------

    def _snapshot_locked(self) -> dict:
        return {
            "source": "keys",
            "running": self._running,
            "supported": self._supported,
            "step_um": self._step_um,
            "step_source": self._step_source,
            "step_um_last": self._step_um_last,
            "counted": self._counted,
            "ignored_not_slacs": self._ignored_not_slacs,
            "last_key": self._last_key,
            "slacs_in_front": self._slacs_in_front,
            "error": self._error,
            "flip_x": self._flip_x,
            "flip_y": self._flip_y,
        }

    def snapshot(self) -> dict:
        """Follower state plus SLACS's controls as they read now (None without a reader)."""
        reading = self._controls_reading()  # outside the lock: it calls into the reader
        with self._lock:
            return {**self._snapshot_locked(), **reading}


__all__ = ["SlacsKeyFollower", "KEY_BINDINGS", "KEY_VIEW_SIGN", "MARK_KEYS", "BLOCKING_MODIFIERS",
           "MAX_STEP_UM", "STEP_SOURCES", "HOLD_RESET_SECONDS", "SCRIPT_NAME"]
