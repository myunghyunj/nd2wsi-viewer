"""Platform capabilities required by specific filesystem tests."""

import os
import sys

import pytest


@pytest.fixture(autouse=True, scope="session")
def session_logs_stay_in_the_test_run(tmp_path_factory):
    """app.main() opens a session log; keep it out of the user's log folder."""
    name = "ND2WSI_SESSION_LOG_DIR"
    previous = os.environ.get(name)
    os.environ[name] = str(tmp_path_factory.mktemp("session-logs"))
    yield
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


@pytest.fixture(autouse=True)
def session_log_closed_after_each_test():
    """app.main() leaves its session log open for the atexit hook; close it per test."""
    yield
    session_log = sys.modules.get("nd2wsi.session_log")
    if session_log is not None:
        session_log.stop("test finished")


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
