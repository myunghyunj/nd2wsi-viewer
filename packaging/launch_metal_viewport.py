"""Separate, opt-in macOS native viewport. Every launch creates a fresh Agent.

Automation must launch this executable, identify its Agent session, and never
reuse a User window. No updater, document associations, cache builder, browser
server, or annotation writer is started by this entry point.
"""

import multiprocessing
import sys

if "-c" in sys.argv[:-1]:
    exec(sys.argv[sys.argv.index("-c") + 1])
    sys.exit(0)
multiprocessing.freeze_support()

if __name__ == "__main__":
    from nd2wsi.metal_viewport.launcher import main

    raise SystemExit(main())
