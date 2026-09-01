#!/usr/bin/env python3
"""Fetch the example acquisitions from the immutable testdata release.

Two real Nikon Ti2 scans back the real-data test suite and the build's
smoke gate. They are too large to live in git, so they hang off the
``testdata-v1`` GitHub release, pinned by SHA-256 here. Files that already
exist and match are left alone; partial downloads never land on the final
path.

    python scripts/fetch_testdata.py [--dest docs]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/myunghyunj/nd2wsi-viewer/releases/download/testdata-v1"
FILES = {
    "example_cell.nd2": "51d603317947b04c3b43c8e694f3ba97a16478ce30e795857ec6ae0321d41c33",
    "example_tissue.nd2": "902fd98e74a348a5402af68c161fe305bc1b2458e0f6cb087d94c76357c2660b",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", default="docs", help="directory to place the files in")
    args = ap.parse_args(argv)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    for name, want in FILES.items():
        out = dest / name
        if out.exists():
            if sha256(out) == want:
                print(f"{name}: present and verified")
                continue
            print(f"{name}: exists but does not match, refetching")
        tmp = out.with_suffix(out.suffix + ".partial")
        url = f"{RELEASE}/{name}"
        print(f"{name}: downloading …")
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as fh:
                while True:
                    block = r.read(1 << 20)
                    if not block:
                        break
                    fh.write(block)
            if sha256(tmp) != want:
                raise ValueError("checksum mismatch after download")
            tmp.replace(out)
            print(f"{name}: fetched and verified")
        except Exception as e:
            tmp.unlink(missing_ok=True)
            print(f"{name}: FAILED ({e})", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
