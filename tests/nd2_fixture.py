"""Synthetic single-frame ND2 files, written by scripts/make_synthetic_nd2.py.

Every test writes its own file. limnd2 writes these few-megabyte fixtures in
about 3 ms, no slower than copying a session-built template, and a fresh write
gives each test its own source mtime and inode, which the cache staleness and
source-replacement tests depend on. Callers keep ``pytest.importorskip("limnd2")``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_synthetic_nd2.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("make_synthetic_nd2", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_script = _load_script()


def write_synthetic_nd2(
    path: Path,
    img: np.ndarray,
    channels: list[tuple[str, str]],
    *,
    pixel_um: float,
    magnification: float | None = 20.0,
) -> Path:
    """Write ``img`` (Y, X, C) as one frame whose C planes carry ``channels``.

    ``channels`` is a list of (name, color). No modality is recorded, and the
    objective magnification is left out when ``magnification`` is None.
    """
    return _script.write_nd2(
        path, img, channels, pixel_um, magnification=magnification, modality=None
    )
