"""Original-name macOS release entry with capability-based display selection."""
import multiprocessing
import sys

if "-c" in sys.argv[:-1]:
    exec(sys.argv[sys.argv.index("-c") + 1])
    sys.exit(0)
multiprocessing.freeze_support()

if __name__ == "__main__":
    from nd2wsi.desktop import main

    raise SystemExit(main())
