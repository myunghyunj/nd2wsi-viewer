"""Platform capabilities required by specific filesystem tests."""

import os

import pytest


@pytest.fixture
def symlink_support(tmp_path):
    """Skip only symlink tests when Windows has not granted that privilege."""
    if os.name != "nt":
        return
    target = tmp_path / ".symlink-capability-target"
    link = tmp_path / ".symlink-capability-link"
    target.write_bytes(b"probe")
    try:
        try:
            link.symlink_to(target)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink privilege is unavailable; junction and lock tests still run")
            raise
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
