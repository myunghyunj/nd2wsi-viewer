"""PyInstaller entry point for the nd2wsi-viewer macOS app."""
import multiprocessing
import sys

# Libraries under the hood start multiprocessing helpers (resource_tracker)
# by re-executing this binary with python-style arguments. Without these two
# guards the helper relaunches the whole app, which shows a second window
# and leaves a zombie that later breaks Finder launches with error -600.
if "-c" in sys.argv[:-1]:
    code = sys.argv[sys.argv.index("-c") + 1]
    exec(code)
    sys.exit(0)

multiprocessing.freeze_support()

from nd2wsi.app import main

if __name__ == "__main__":
    sys.exit(main())
