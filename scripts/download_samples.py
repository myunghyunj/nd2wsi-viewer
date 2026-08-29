#!/usr/bin/env python3
"""Download small real-world ND2 samples from OME's public image collection.

See https://downloads.openmicroscopy.org/images/ND2/ for licensing (CC-BY /
per-folder COPYING files) and more files.
"""
import urllib.request
from pathlib import Path

SAMPLES = {
    # modern container, T=65 Z=9 single channel uint16, 48 MB
    "control002.nd2": "https://downloads.openmicroscopy.org/images/ND2/jonas/control002.nd2",
    # tiny modern brightfield-ish uint16 single frame, 0.3 MB
    "BF007.nd2": "https://downloads.openmicroscopy.org/images/ND2/maxime/BF007.nd2",
    # legacy JPEG2000-era multipoint (P=5, C=2) with stage positions, 16 MB
    "but3_cont200-1.nd2": "https://downloads.openmicroscopy.org/images/ND2/aryeh/but3_cont200-1.nd2",
}

if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "testdata"
    out.mkdir(exist_ok=True)
    for name, url in SAMPLES.items():
        dest = out / name
        if dest.exists():
            print("have", dest)
            continue
        print("downloading", url)
        urllib.request.urlretrieve(url, dest)
        print("  ->", dest, dest.stat().st_size, "bytes")
