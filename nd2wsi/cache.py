"""Cache identity, atomic construction, and the managed data folder.

A viewing cache is an app-owned artifact derived from one slide and one
T/P/Z selection. Before 0.8 it was identified by the slide's stem alone,
written straight into its final path, and reused on nothing more than
existence — so a crash wedged the slide, a changed file was silently
served stale, and two selections fought over one directory.

Now every cache lives in a container named for its source and selection,

    <slide dir>/nd2wsi/caches/<source-name>--t0-p0-zmid.nd2wsi-cache/
        manifest.json     identity, fingerprint, completion — written last
        store.ome.zarr/   the pyramid

built in a staging sibling under an interprocess lock and renamed into
place only when complete. A cache that fails validation is quarantined,
never deleted outright, and a store the user opened directly is never
touched at all. Annotations are work, not cache; they live one folder
over in nd2wsi/annotations/ and no cache operation deletes them.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .session_lock import SessionFileLock

MANAGED_DIR = "nd2wsi"
CACHES_DIR = "caches"
ANNOTATIONS_DIR = "annotations"
CACHE_SUFFIX = ".nd2wsi-cache"
STORE_NAME = "store.ome.zarr"
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "nd2wsi-cache/2"
# overview containers hold no level 0, which a pre-0.9 reader would serve
# as a full store at half resolution with level-0 calibration — so they
# carry a format that old readers reject (and quarantine) instead
OVERVIEW_FORMAT = "nd2wsi-cache/3"
KNOWN_FORMATS = (MANIFEST_FORMAT, OVERVIEW_FORMAT)
ALGORITHM = "box-mean-floor-v1"

# A lock names the machine and boot that took it. A drive carried to
# another computer brings its lockfiles along, and a pid from the other
# machine means nothing here: the builder cannot be alive on this side.
# The same goes for a lock from before a reboot, so the boot time is part
# of the identity and no network hardware lookup is needed.


def _machine_id() -> str:
    try:
        booted = round((time.time() - time.clock_gettime(time.CLOCK_UPTIME_RAW)) / 10)
    except (AttributeError, OSError, ValueError):
        booted = 0
    return hashlib.sha1(f"{socket.gethostname()}|{booted}".encode()).hexdigest()[:12]


_MACHINE_ID = _machine_id()
_FOREIGN_LOCK_STALE_S = 60.0


class CacheFromNewerApp(RuntimeError):
    """A complete cache written in a format this build does not know."""


def _format_number(fmt: Any) -> int | None:
    m = re.fullmatch(r"nd2wsi-cache/(\d+)", str(fmt or ""))
    return int(m.group(1)) if m else None


def newer_cache_format(container: str | Path) -> str | None:
    """The manifest format if it is newer than this build reads, else None.

    An older app must never quarantine and rebuild a cache a newer app
    wrote: the cache is not broken, the app is behind. The caller refuses
    to open and names the update instead of destroying work.
    """
    try:
        m = json.loads((Path(container) / MANIFEST_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(m, dict) or not m.get("complete"):
        return None
    number = _format_number(m.get("format"))
    newest_known = max(_format_number(f) or 0 for f in KNOWN_FORMATS)
    return str(m.get("format")) if number is not None and number > newest_known else None


def selection_tag(selection: Any) -> str:
    """``t0-p0-zmid`` — the requested selection, as named in the path."""
    sel = selection.describe() if hasattr(selection, "describe") else dict(selection)
    return f"t{sel.get('t', 0)}-p{sel.get('p', 0)}-z{sel.get('z', 'mid')}"


def managed_dir(slide: str | Path) -> Path:
    return Path(slide).parent / MANAGED_DIR


def annotations_dir(slide: str | Path) -> Path:
    return managed_dir(slide) / ANNOTATIONS_DIR


def source_tag(slide: str | Path) -> str:
    """Filesystem-safe source name, including its extension.

    Keeping ``.nd2`` or ``.svs`` in the key prevents two files with the same
    stem from sharing a cache container.
    """
    return re.sub(r'[\/:*?"<>|\x00-\x1f]+', "_", Path(slide).name)


def legacy_cache_container(
    slide: str | Path, selection: Any = None, cache_dir: str | Path | None = None
) -> Path:
    """The stem-only managed path written by versions 0.8 and 0.9."""
    from .reader import PlaneSelection

    slide = Path(slide)
    base = Path(cache_dir) if cache_dir else managed_dir(slide) / CACHES_DIR
    tag = selection_tag(selection or PlaneSelection())
    return base / f"{slide.stem}--{tag}{CACHE_SUFFIX}"


def cache_container(
    slide: str | Path, selection: Any = None, cache_dir: str | Path | None = None
) -> Path:
    """The v1 cache path, keyed by source filename and selected plane."""
    from .reader import PlaneSelection

    slide = Path(slide)
    base = Path(cache_dir) if cache_dir else managed_dir(slide) / CACHES_DIR
    tag = selection_tag(selection or PlaneSelection())
    return base / f"{source_tag(slide)}--{tag}{CACHE_SUFFIX}"


def container_store(container: str | Path) -> Path:
    return Path(container) / STORE_NAME


def quick_fingerprint(path: str | Path) -> dict[str, Any]:
    """Identity of a source file, cheap enough to take on every open.

    Size, mtime, and a hash over the head, middle and tail megabyte. This
    is a stale-cache detector, not a full content hash: a change that
    leaves size, mtime and all three sampled windows intact goes unseen.
    """
    path = Path(path)
    st = path.stat()
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for offset in (
            0,
            max(0, st.st_size // 2 - (1 << 19)),
            max(0, st.st_size - (1 << 20)),
        ):
            fh.seek(offset)
            h.update(fh.read(1 << 20))
    h.update(str(st.st_size).encode())
    return {
        "name": path.name,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "quick_sha256": h.hexdigest(),
    }


def fingerprints_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in ("size", "mtime_ns", "quick_sha256"))


def write_manifest(
    container: Path,
    slide: Path,
    fingerprint: dict[str, Any],
    selection: dict[str, Any],
    resolved_z: Any,
    tile: int,
    shape_cyx: tuple[int, int, int],
    dtype: str,
    kind: str = "full",
    storage: dict[str, Any] | None = None,
) -> None:
    from . import __version__

    manifest = {
        "format": MANIFEST_FORMAT if kind == "full" else OVERVIEW_FORMAT,
        "kind": kind,
        "complete": True,
        "generation": uuid.uuid4().hex,
        "source": {
            **fingerprint,
            "relative_path": os.path.relpath(slide, container),
        },
        "selection": {**selection, "z_resolved": resolved_z},
        "image": {"shape_cyx": list(shape_cyx), "dtype": dtype},
        "pyramid": {"algorithm": ALGORITHM, "tile": tile},
        "storage": storage
        or {
            "format": "zarr",
            "zarr_version": 2,
            "ngff_version": "0.4",
            "backend": "zarr-v2-direct",
        },
        "created_by": {"nd2wsi_version": __version__},
    }
    tmp = container / f".{MANIFEST_NAME}.tmp"
    tmp.write_text(json.dumps(manifest, indent=1))
    tmp.replace(container / MANIFEST_NAME)


def read_manifest(container: str | Path) -> dict[str, Any] | None:
    try:
        m = json.loads((Path(container) / MANIFEST_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return m if isinstance(m, dict) and m.get("complete") else None


def cache_matches(
    container: Path, slide: Path, selection: Any, kind: str | None = None
) -> bool:
    """A cache is valid for this slide and selection, or it is stale.

    ``kind`` narrows the match ("full" or "overview"); None takes either.
    """
    m = read_manifest(container)
    if m is None or m.get("format") not in KNOWN_FORMATS:
        return False
    if m.get("pyramid", {}).get("algorithm") != ALGORITHM:
        return False
    if kind is not None and m.get("kind", "full") != kind:
        return False
    if m.get("source", {}).get("name") not in (None, slide.name):
        return False
    sel = selection.describe() if hasattr(selection, "describe") else dict(selection)
    stored = m.get("selection", {})
    if any(str(stored.get(k)) != str(v) for k, v in sel.items()):
        return False
    try:
        return fingerprints_match(m.get("source", {}), quick_fingerprint(slide))
    except OSError:
        return False


def quarantine(path: Path) -> Path:
    """Set a broken artifact aside instead of destroying it."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 0
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
    path.rename(target)
    return target


class CacheLock:
    """One build per source and selection, across processes.

    The JSON lockfile beside the container remains the public protocol for
    older nd2wsi-viewer builds.  Current builds additionally serialize each
    inspect/claim transaction with a persistent kernel-locked arbiter inode.
    A stale JSON lock is claimed in place through an open descriptor, and
    release writes a stale tombstone through that same descriptor instead of
    unlinking by pathname. Consequently current-build contenders cannot remove
    a successor's lock. Older builds still understand the JSON/tombstone, but
    cannot participate in the new arbiter protocol.
    """

    def __init__(self, container: Path):
        self.path = container.with_name(container.name + ".lock")
        self._gen: str | None = None
        self._fd: int | None = None
        self._arbiter = SessionFileLock(
            self.path.with_name(self.path.name + ".arbiter")
        )
        self._mutex = threading.RLock()

    def _read(self, path: Path | None = None) -> dict[str, Any] | None:
        try:
            info = json.loads((path or self.path).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return info if isinstance(info, dict) else None

    @staticmethod
    def _read_fd(fd: int) -> tuple[dict[str, Any] | None, os.stat_result]:
        st = os.fstat(fd)
        try:
            info = json.loads(os.pread(fd, st.st_size, 0))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            info = None
        return (info if isinstance(info, dict) else None), st

    @staticmethod
    def _write_fd(fd: int, payload: bytes) -> None:
        """Replace one opened lock inode's small payload without path lookup."""
        view = memoryview(payload)
        offset = 0
        while view:
            written = os.pwrite(fd, view, offset)
            if written <= 0:
                raise OSError("short write while updating cache lock")
            offset += written
            view = view[written:]
        os.ftruncate(fd, offset)

    def _path_is_fd(self, fd: int) -> bool:
        try:
            held = os.fstat(fd)
            current = os.stat(self.path, follow_symlinks=False)
        except OSError:
            return False
        return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)

    @staticmethod
    def _safe_lock_inode(st: os.stat_result) -> bool:
        """Only lockfiles with one regular pathname are safe to overwrite."""
        return stat.S_ISREG(st.st_mode) and st.st_nlink == 1

    @staticmethod
    def _payload(gen: str, *, released: bool = False) -> bytes:
        info: dict[str, Any] = {
            "pid": None if released else os.getpid(),
            "time": time.time(),
            "gen": gen,
            "machine": _MACHINE_ID,
        }
        if released:
            info["released"] = True
        return json.dumps(info).encode()

    def _abandon_claim(self, fd: int, gen: str) -> None:
        """Best-effort stale marker after a claim write could not complete."""
        try:
            self._write_fd(fd, self._payload(gen, released=True))
            return
        except OSError:
            pass
        try:
            os.ftruncate(fd, 0)
            os.utime(fd, (0, 0))
        except OSError:
            # If even invalidation fails, the ordinary malformed-lock grace is
            # still preferable to unlinking a pathname a legacy process may
            # have replaced concurrently.
            pass

    def _install_claim(self, fd: int, payload: bytes, gen: str) -> bool:
        """Install a claim, never leaving a valid live orphan on failure."""
        try:
            self._write_fd(fd, payload)
        except OSError:
            # A final truncate can fail after the complete JSON already landed.
            # In that case the claim is usable and must be retained rather than
            # abandoned as an unowned live-PID lock.
            try:
                info, _ = self._read_fd(fd)
            except OSError:
                info = None
            if (
                info is not None
                and info.get("gen") == gen
                and info.get("pid") == os.getpid()
                and info.get("machine") == _MACHINE_ID
                and info.get("released") is not True
                and self._path_is_fd(fd)
            ):
                return True
            self._abandon_claim(fd, gen)
            raise

        if self._path_is_fd(fd):
            return True
        # A legacy process replaced the pathname while ignoring the current
        # arbiter. Make our now-detached inode stale before closing it.
        self._abandon_claim(fd, gen)
        return False

    @staticmethod
    def _stale_info(info: dict[str, Any] | None, mtime: float) -> bool:
        if info is None:
            # unreadable or momentarily empty: only old age makes it stale
            return time.time() - mtime > 30
        if info.get("released") is True:
            return True
        machine = info.get("machine")
        if machine is not None and machine != _MACHINE_ID:
            # taken on another machine: its process cannot be running
            # here, and only clock skew argues for a short grace
            try:
                age = time.time() - float(info.get("time", 0))
            except (TypeError, ValueError, OverflowError):
                age = time.time() - mtime
            return age > _FOREIGN_LOCK_STALE_S
        # A current or older viewer may hold this compatibility lock for a
        # whole session.  A PID proven live on this machine therefore wins
        # over wall-clock age; only a dead/invalid PID is reclaimable.
        pid = info.get("pid")
        try:
            pid = int(pid)
            if pid <= 0:
                return True
            os.kill(pid, 0)
            return False
        except (ProcessLookupError, TypeError, ValueError, OverflowError):
            return True
        except PermissionError:
            return False

    def _stale(self) -> bool:
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return False
        try:
            info, st = self._read_fd(fd)
            return self._stale_info(info, st.st_mtime)
        finally:
            os.close(fd)

    def acquire(self, timeout: float = 3600.0, poll: float = 0.5) -> None:
        timeout = max(0.0, float(timeout))
        poll = max(0.001, float(poll))
        deadline = time.monotonic() + timeout
        with self._mutex:
            if self._fd is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            gen = uuid.uuid4().hex
            while True:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    self._arbiter.acquire(timeout=remaining, poll=poll)
                except TimeoutError:
                    raise TimeoutError(
                        f"another process is building {self.path.stem}"
                    ) from None

                fd: int | None = None
                acquired = False
                try:
                    # Stamp the claim only after waiting for the arbiter. A
                    # freshly acquired lock must not inherit its wait time.
                    payload = self._payload(gen)
                    try:
                        fd = os.open(
                            self.path,
                            os.O_CREAT | os.O_EXCL | os.O_RDWR,
                        )
                        acquired = self._install_claim(fd, payload, gen)
                    except FileExistsError:
                        try:
                            fd = os.open(
                                self.path,
                                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                            )
                        except FileNotFoundError:
                            fd = None
                        except OSError as exc:
                            # A symlink or other unsafe pathname is an occupied
                            # lock, not a target to follow or overwrite.
                            if exc.errno == errno.ELOOP:
                                fd = None
                            else:
                                raise
                        if fd is not None:
                            info, st = self._read_fd(fd)
                            if self._safe_lock_inode(st) and self._stale_info(
                                info, st.st_mtime
                            ):
                                # Claim the exact stale inode we inspected. If
                                # an older build replaced the path meanwhile,
                                # this write lands only on the detached inode.
                                acquired = self._install_claim(fd, payload, gen)

                    if acquired and fd is not None:
                        self._fd = fd
                        self._gen = gen
                        fd = None
                        return
                finally:
                    if fd is not None:
                        os.close(fd)
                    self._arbiter.release()

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"another process is building {self.path.stem}"
                    ) from None
                time.sleep(min(poll, remaining))

    def refresh(self) -> bool:
        """Renew this lock's timestamp without touching a successor's lock.

        The descriptor retained at acquisition pins the owned inode. A
        concurrent legacy replacement receives a different inode and is never
        overwritten. The generation stored on the opened inode remains the
        ownership authority.
        """
        with self._mutex:
            gen, fd = self._gen, self._fd
            if gen is None or fd is None:
                return False
            try:
                self._arbiter.acquire(timeout=5.0, poll=0.05)
            except (TimeoutError, OSError):
                return False
            try:
                try:
                    info, _ = self._read_fd(fd)
                    if info is None or info.get("gen") != gen:
                        return False
                    info["time"] = time.time()
                    self._write_fd(fd, json.dumps(info).encode())
                    return self._path_is_fd(fd)
                except OSError:
                    return False
            finally:
                self._arbiter.release()

    def release(self) -> None:
        with self._mutex:
            gen, fd = self._gen, self._fd
            if gen is None or fd is None:
                return
            # Serialize the tombstone with current-build claim attempts. If the
            # arbiter is unavailable, close locally without writing: a contender
            # may already have this same inode open, and an unsynchronized late
            # truncate could corrupt its successor claim. That rare path fails
            # closed (the live-PID JSON remains until process exit) but must not
            # prevent outer cleanup such as releasing the plate session lock.
            arbiter_acquired = False
            try:
                try:
                    self._arbiter.acquire(timeout=30.0, poll=0.05)
                    arbiter_acquired = True
                except (TimeoutError, OSError):
                    pass
                if arbiter_acquired:
                    try:
                        info, _ = self._read_fd(fd)
                        if info is not None and info.get("gen") == gen:
                            # Keep the pathname/inode intact and make it immediately
                            # reclaimable by both current and legacy builds. Writing
                            # through ``fd`` cannot affect a successor that replaced it.
                            info.update(
                                {"pid": None, "time": time.time(), "released": True}
                            )
                            self._write_fd(fd, json.dumps(info).encode())
                    except OSError:
                        # With the arbiter held, invalidating this exact inode is
                        # safe; the malformed-lock grace can then reclaim it.
                        try:
                            os.ftruncate(fd, 0)
                            os.utime(fd, (0, 0))
                        except OSError:
                            pass
            finally:
                try:
                    if arbiter_acquired:
                        self._arbiter.release()
                except OSError:
                    pass
                finally:
                    self._gen = None
                    self._fd = None
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def __enter__(self) -> CacheLock:
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def commit_container(staging: Path, final: Path) -> None:
    """Expose a finished staging container at the final path atomically.

    Same-directory renames only. If a previous container exists it is set
    aside first and removed after the new one is in place; a failure puts
    it back.
    """
    backup = None
    if final.exists():
        backup = final.with_name(final.name + f".replaced-{uuid.uuid4().hex[:8]}")
        final.rename(backup)
    try:
        staging.rename(final)
    except OSError:
        if backup is not None:
            backup.rename(final)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def sweep_stale_builds(caches_dir: Path) -> int:
    """Remove leftover staging and backup dirs whose builder is gone.

    Covers ``*.building-<pid>`` staging and ``*.replaced-*`` backups that
    a crash inside commit_container can strand. Liveness is judged from
    the family's exact lockfile — names are never fed to glob, so a
    bracketed NIS-Elements filename cannot inject wildcards.
    """
    n = 0
    if not caches_dir.is_dir():
        return 0
    for item in caches_dir.iterdir():
        name = item.name
        if ".building-" in name:
            family = name.split(".building-")[0].lstrip(".")
        elif ".replaced-" in name:
            family = name.split(".replaced-")[0]
        else:
            continue
        lock = CacheLock(caches_dir / family)
        info = lock._read()
        live = False
        if info:
            machine = info.get("machine")
            if machine is not None and machine != _MACHINE_ID:
                live = False  # a builder on another machine is not running here
            else:
                try:
                    os.kill(int(info.get("pid", -1)), 0)
                    live = True
                except (ProcessLookupError, ValueError, TypeError):
                    pass
                except PermissionError:
                    live = True
        if not live:
            shutil.rmtree(item, ignore_errors=True)
            n += 1
    return n
