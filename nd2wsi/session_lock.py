"""Kernel-backed lock for a writer that lives for the viewer session.

``CacheLock`` is appropriate for bounded build/rename sections, but its stale
lockfile policy is not suitable for a writer that may legitimately stay open
for many hours.  This lock keeps a file descriptor open and lets the kernel
own exclusivity.  The lock file is deliberately never unlinked: keeping one
inode avoids two contenders locking different files under the same pathname.

The JSON payload is diagnostic only.  Lock ownership depends exclusively on
``flock`` and is released when the descriptor is closed, including on process
exit.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import stat
import time
import uuid
from pathlib import Path
from typing import Any


class SessionFileLock:
    """An exclusive, persistent-inode advisory file lock."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None
        self._owner = uuid.uuid4().hex

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self, timeout: float = 0.0, poll: float = 0.05) -> None:
        """Acquire the lock or raise ``TimeoutError`` after ``timeout``.

        Timeout accounting is monotonic, so wall-clock adjustments and stale
        metadata can never make a live lock expire.
        """

        if self._fd is not None:
            return

        timeout = max(0.0, float(timeout))
        poll = max(0.001, float(poll))
        deadline = time.monotonic() + timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        while True:
            fd = os.open(self.path, flags, 0o600)
            locked = False
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise OSError(
                        errno.EPERM,
                        "lock coordination path must be a regular, singly linked file",
                        str(self.path),
                    )
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except OSError as exc:
                        if exc.errno == errno.EINTR:
                            continue
                        if exc.errno not in (errno.EACCES, errno.EAGAIN):
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                f"another viewer is writing {self.path.stem}"
                            ) from None
                        time.sleep(min(poll, remaining))

                try:
                    current = os.stat(self.path, follow_symlinks=False)
                    path_is_opened_inode = (opened.st_dev, opened.st_ino) == (
                        current.st_dev,
                        current.st_ino,
                    )
                except OSError:
                    path_is_opened_inode = False
                if not path_is_opened_inode:
                    # The pathname changed between open and flock. Never hold a
                    # detached inode while a contender locks its replacement.
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    locked = False
                    os.close(fd)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"lock coordination path changed for {self.path.stem}"
                        ) from None
                    time.sleep(min(poll, remaining))
                    continue

                self._fd = fd
                self._write_metadata(fd)
                return
            except BaseException:
                if self._fd == fd:
                    self._fd = None
                if locked:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

    def _write_metadata(self, fd: int) -> None:
        """Best-effort diagnostics; failure does not affect lock ownership."""

        payload: dict[str, Any] = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": time.time(),
            "owner": self._owner,
        }
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write while recording lock metadata")
                view = view[written:]
            os.fsync(fd)
        except OSError:
            # The open descriptor and flock are the authority.  Diagnostics
            # must never turn a successfully held kernel lock into a failure.
            pass

    def release(self) -> None:
        """Release the kernel lock without unlinking its coordination file."""

        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> SessionFileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()
