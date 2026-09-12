# Native viewer automation boundary

When an agent uses the macOS viewer, it MUST open a **new Agent window** before
opening slides or changing viewer state. Use `pywebview.api.new_window("agent")`
from the native bridge, **File → New Agent Window** (Shift-Command-N), or launch
the same app executable with `--agent-window`. Never reuse the user's current
window or its tabs. Read `pywebview.api.window_context()` in the new window and
verify `role == "agent"` and a fresh session `id` before continuing.

- Identify windows by role and session ID, not only the app name or first window.
  Some automation tools resolve a bundle ID to only the first process. If that
  is a User window, stop UI actions; do not use it as a substitute for the Agent
  window. Use an unambiguous Agent-scoped interface or request coordination.
- Agent annotations are private snapshots. Keep generated outputs in that
  window's `exports_root`. Do not merge results into user sidecars without
  explicit approval, and never discard conflict drafts or manual ROIs.
- Do not close, reload, retarget, change LUTs, or switch tabs in a User window.
- OS keyboard and mouse focus remains shared. Prefer the dedicated window's
  bridge/server; coordinate foreground interaction rather than assuming separate
  windows make simultaneous pointer input safe.
- Do not remove/rebuild shared research caches from an Agent window to benchmark.
  Use explicitly isolated test data/output locations.
- Keep these automation directions in code and this internal file, **not README.md**.
- This experimental branch is macOS-only. Do not publish a Windows update or
  replace an active installed app as part of local validation.
