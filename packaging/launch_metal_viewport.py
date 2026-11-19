"""Separate, opt-in macOS native viewport. Every launch creates a fresh Agent.

Automation must launch this executable, identify its Agent session, and never
reuse a User window. No updater, document associations, cache builder, browser
server, or annotation writer is started by this entry point.
"""

import multiprocessing
import re
import sys

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

if __name__ == "__main__":
    from nd2wsi.metal_viewport.launcher import main

    raise SystemExit(main())
