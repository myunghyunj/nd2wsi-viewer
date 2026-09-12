# macOS multiwindow beta 2.1.0b2

This local beta adds multiple native windows to the existing Metal beta. It does
not publish a stable release or a Windows update. README.md is unchanged.

## Behavior and isolation

- File → New Window (Command-N) starts a separate User process/window.
- File → New Agent Window (Shift-Command-N) starts a separate Agent process/window.
- Every native title and in-viewer window menu identifies the role and session ID.
- Each process owns its tabs, localhost port/capability, native gestures, viewer
  state and private WebView. Closing a parent does not terminate child processes.
  Frozen children use `PYINSTALLER_RESET_ENVIRONMENT=1` and detached process sessions.
- User windows save canonical annotations with a cross-process lock and exact
  sidecar revision check. A stale save returns 409 instead of overwriting another
  window's changes. The attempted annotation document is preserved as a unique
  recovery draft under that window's session, unless the disk cannot be written;
  in that case the window remains open with unsaved data and an error.
- Agent windows snapshot original annotation JSON bytes into a private session,
  without migrating legacy originals. Subsequent edits stay private. There is no
  automatic merge. Site annotations retain the existing P-only ownership across
  T/Z; ordinary slides retain their existing plane identity.
- Agent windows do not build/repair shared caches, and open plate sources with
  persistent caching disabled. Existing valid WSI caches and native supported
  SVS files can be read. An uncached WSI must first have its cache prepared outside
  the Agent session. Shared cache deletion is disabled in all managed windows.
- Agent export filenames include a session prefix. Native save dialogs still
  control the final destination; this is not an OS filesystem sandbox.
- Native close and tab close await annotation-save acknowledgement; conflict,
  loading, timeout or active export prevents destructive UI teardown. Each server
  drains its own outstanding resource users. In-process application replacement
  is disabled while this multiwindow coordination model is in use.

## Limits

Independent windows do not isolate the OS mouse/keyboard focus. Automation
directions are in `AGENTS.md`, code docstrings and `window_context()`, not README.
Tools which can select only one process per bundle ID must not silently target
the first User window. This is also not crash-proof recovery of keystrokes still
inside the 800ms autosave debounce, nor coordination with older app binaries that
do not implement revision checks. Agent snapshot isolation does protect the
canonical user annotations from Agent edits even alongside an older viewer.

## Validation on Apple M4 Pro, 2026-09-12

- Full suite: **982 passed, 9 skipped**, with the same 122 existing ND2/NumPy
  deprecation warnings. New targeted window/session/annotation/closing tests:
  **57 passed**. Post-review relevant annotation/launch/export rerun: **45 passed**.
- Separate-process annotation race: exactly one save succeeds and one conflicts;
  conflicting data remains recoverable. Agent byte-identical snapshots, missing
  and legacy sidecars, plate sites, disk-full draft failure, cache restrictions,
  stale HTTP requests and frontend A→B→A transitions are covered.
- Packaged GPU self-test passes on the actual M4 Pro. Packaged ND2/SVS scientific
  exports preserve pixels/calibration/channels. That smoke reuses its existing
  ND2 cache, so its reducer count is zero; GPU self-test is separate.
- A User beta window is visibly running with its role/ID and File menu. The user
  began using that window during validation; it was left untouched thereafter.
- A separate Agent process was started. A further isolated packaged Agent GUI
  self-test opened the copied example, verified WKWebView bridge/version/slide,
  and closed only its own window successfully (0.85s to verification). Its
  session reached `closed` while the user's process remained live.
- Physical menu/shortcut dispatch could not be independently exercised after the
  user took over the User window: the computer-use provider selected that first
  process even while a second Agent process was alive. Native menu construction
  and launch/close routing have source-level tests; no unsupported claim of a
  complete interactive shortcut test is made.

## Primary implementation references

- [PyInstaller independent subprocess runtime](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#using-sys-executable-to-spawn-subprocesses-that-outlive-the-application-process-implementing-application-restart)
- [pywebview closing event and veto](https://pywebview.flowrl.com/api/#window-events-closing)

This is ad-hoc signed local software, not Apple-notarized. Current User windows,
the installed stable app, source research files and public updater feeds are not
replaced by the build or validation workflow.
