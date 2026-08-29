"""PyInstaller entry point for the nd2wsi-viewer macOS app."""
import sys

from nd2wsi.app import main

if __name__ == "__main__":
    sys.exit(main())
