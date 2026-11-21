"""Per-window annotation snapshots and compare-and-swap sidecar writes.

The stable/Windows server does not use this module unless given a window
session. Agent snapshots preserve the entire original JSON byte-for-byte;
neither their locks nor their edits are placed beside the source slide.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from .session_lock import SessionFileLock

_LOCK_TIMEOUT = 10.0
_READONLY_ERRNOS = (errno.EACCES, errno.EPERM, errno.EROFS)
_MAX_ANNOTATION_BYTES = 64 * 1024 * 1024


def _is_windows_reparse_point(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _read(path: Path) -> bytes | None:
    try:
        lstat = os.lstat(str(path))
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(lstat.st_mode) or _is_windows_reparse_point(lstat):
        raise OSError(errno.ELOOP, "Refusing to read symlinked annotation sidecar")
    if not stat.S_ISREG(lstat.st_mode):
        raise OSError(errno.EINVAL, "Annotation sidecar is not a regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OSError(errno.ELOOP, "Refusing to read symlinked annotation sidecar") from exc
        raise
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or _is_windows_reparse_point(st)
            or (st.st_ino != 0 and lstat.st_ino != 0 and (st.st_dev, st.st_ino) != (lstat.st_dev, lstat.st_ino))
        ):
            raise OSError(errno.EINVAL, "Annotation sidecar is not a regular file")
        if st.st_size > _MAX_ANNOTATION_BYTES:
            raise OSError(errno.EFBIG, "Annotation sidecar exceeds size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_ANNOTATION_BYTES:
                raise OSError(errno.EFBIG, "Annotation sidecar exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def revision(raw: bytes | None) -> str:
    return "missing" if raw is None else "sha256:" + hashlib.sha256(raw).hexdigest()


@contextmanager
def annotation_lock(path: Path):
    """Lock a persistent inode, not the sidecar replaced by atomic saves."""
    lock = SessionFileLock(path.with_name("." + path.name + ".window-lock"))
    lock.acquire(timeout=_LOCK_TIMEOUT)
    try:
        yield
    finally:
        lock.release()


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


class AnnotationConflict(Exception):
    def __init__(self, current_revision: str, draft_path: Path | None, draft_error: str | None = None):
        super().__init__("Another window changed these annotations. Your edits were not applied; no merge was performed.")
        self.current_revision = current_revision
        self.draft_path = draft_path
        self.draft_error = draft_error


class AnnotationWorkspace:
    def __init__(self, session):
        self.session = session
        self.agent = session.role == "agent"
        self._canonical: dict[Path, Path] = {}

    def snapshot(self, canonical: Path, *, source: Path | None = None) -> Path:
        """Snapshot once by canonical identity, including a missing sidecar.

        Source reads do not create a canonical lock/directory or migrate a
        legacy sidecar. An atomic canonical replacement yields one full version.
        """
        canonical = canonical.resolve()
        key = hashlib.sha256(os.fsencode(str(canonical))).hexdigest()
        target = Path(self.session.annotation_root) / (key + ".json")
        with annotation_lock(target):
            if not target.exists():
                raw = _read(source or canonical)
                _atomic_write(target, raw if raw is not None else b'{"items": []}\n')
        self._canonical[target] = canonical
        return target

    def read(self, path: Path) -> tuple[dict, str]:
        lock_path = path.with_name("." + path.name + ".window-lock")
        if not path.exists() and not lock_path.exists():
            return {}, revision(None)
        try:
            with annotation_lock(path):
                raw = _read(path)
        except OSError as exc:
            if exc.errno not in _READONLY_ERRNOS:
                raise
            raw = _read(path)
        document = json.loads(raw) if raw is not None else {}
        if not isinstance(document, dict):
            raise ValueError("annotation document must be a JSON object")
        return document, revision(raw)

    def write(self, path: Path, payload: dict, expected: object, *, source_path: Path | None = None) -> str:
        with annotation_lock(path):
            previous = _read(path)
            current = revision(previous)
            if not isinstance(expected, str) or expected != current:
                draft = None
                draft_error = None
                try:
                    draft = Path(self.session.drafts_root) / ("annotation-conflict-" + uuid.uuid4().hex + ".json")
                    _atomic_write(draft, json.dumps({
                        "format": "nd2wsi-annotation-conflict/1",
                        "window": self.session.as_dict(),
                        "sidecar_path": str(path),
                        "canonical_path": str(self._canonical.get(path, path)),
                        "source_path": str(source_path) if source_path is not None else None,
                        "expected_revision": expected,
                        "current_revision": current,
                        "payload": payload,
                        "auto_merge": False,
                    }, indent=1, allow_nan=False).encode("utf-8"))
                except (OSError, TypeError, ValueError) as exc:
                    draft = None
                    draft_error = str(exc)
                raise AnnotationConflict(current, draft, draft_error)
            # Keep imported metadata/extensions as well as exact item IDs and
            # geometries. Only the fields supplied by the server are refreshed.
            document = json.loads(previous) if previous is not None else {}
            if not isinstance(document, dict):
                raise ValueError("annotation document must be a JSON object")
            # What this server writes must be finite; another tool's preserved
            # values are written back exactly as that tool stored them.
            json.dumps(payload, allow_nan=False)
            document.update(payload)
            raw = json.dumps(document, indent=1).encode("utf-8")
            _atomic_write(path, raw)
            return revision(raw)
