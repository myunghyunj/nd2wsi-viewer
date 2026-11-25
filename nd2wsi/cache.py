"""Cache identity, atomic construction, and the managed data folder.

A viewing cache is an app-owned artifact derived from one slide and one
T/P/Z selection. Before 0.8 it was identified by the slide's stem alone,
written straight into its final path, and reused on nothing more than
existence — so a crash wedged the slide, a changed file was silently
served stale, and two selections fought over one directory.

Now every cache lives in a container named for its source and selection,

    <slide dir>/nd2wsi/caches/<source-name>--t0-p0-zmid.nd2svs

The file contains the manifest and compressed Zarr chunks. Legacy
``.nd2wsi-cache`` directories remain readable for compatibility.

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
import numbers
import os
import re
import shutil
import socket
import stat
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from .platform_io import (
    BINARY,
    filesystem_path,
    process_is_alive,
    read_at,
    set_fd_times,
    write_at,
)
from .session_lock import SessionFileLock
from .windows_fs import is_windows_name_surrogate_reparse

MANAGED_DIR = "nd2wsi"
CACHES_DIR = "caches"
ANNOTATIONS_DIR = "annotations"
CACHE_SUFFIX = ".nd2wsi-cache"
SINGLE_FILE_SUFFIX = ".nd2svs"
STORE_NAME = "store.ome.zarr"
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "nd2wsi-cache/2"
# overview containers hold no level 0, which a pre-0.9 reader would serve
# as a full store at half resolution with level-0 calibration — so they
# carry a format that old readers reject (and quarantine) instead
OVERVIEW_FORMAT = "nd2wsi-cache/3"
SINGLE_FILE_FORMAT = "nd2wsi-cache/4"
CORRECTED_SINGLE_FILE_FORMAT = "nd2wsi-cache/5"
KNOWN_FORMATS = (MANIFEST_FORMAT, OVERVIEW_FORMAT, SINGLE_FILE_FORMAT, CORRECTED_SINGLE_FILE_FORMAT)
ALGORITHM = "box-mean-floor-v1"

_EXFAT_TZ_STEP_NS = 15 * 60 * 1_000_000_000
_EXFAT_MAX_TZ_STEPS = 26 * 4
_EXFAT_10MS_NS = 10_000_000
_COARSE_1S_NS = 1_000_000_000
_FAT_2S_NS = 2_000_000_000

# A lock names the machine and boot that took it. A drive carried to
# another computer brings its lockfiles along, and a pid from the other
# machine means nothing here: the builder cannot be alive on this side.
# The same goes for a lock from before a reboot. Each window is its own
# process, so both names must agree across every process of one boot, or
# one window's sweep deletes another's live build: the machine is named by
# its hardware or OS identity, not its host name (DHCP or a Bonjour conflict
# can change that between two windows), and the boot by the system's boot
# id, not a boot time (a clock step would move it). Where there is no boot
# id the pid decides, as it did in 2.5.4.


def _host_identity() -> str | None:
    try:
        if sys.platform == "darwin":
            import ctypes

            uuid_bytes = ctypes.create_string_buffer(16)
            wait = (ctypes.c_long * 2)(1, 0)  # struct timespec: give up after 1 s
            if ctypes.CDLL(None).gethostuuid(uuid_bytes, wait) == 0:
                return uuid_bytes.raw.hex()
        elif os.name == "nt":
            import winreg

            access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, access) as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0]) or None
        else:
            for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                try:
                    found = Path(path).read_text(encoding="ascii").strip()
                except (OSError, UnicodeError):
                    continue
                if found:
                    return found
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return None


def _machine_id() -> str:
    """A hash naming this computer; the raw identity never reaches a lockfile."""
    identity = _host_identity()
    if identity is None:
        return hashlib.sha1(socket.gethostname().encode()).hexdigest()[:12]
    return hashlib.sha1(f"nd2wsi-lock|{identity}".encode()).hexdigest()[:12]


def _boot_id() -> str | None:
    """The system's identity of this boot, unchanged by sleep or clock steps."""
    try:
        if sys.platform == "darwin":
            import ctypes

            value = ctypes.create_string_buffer(64)
            size = ctypes.c_size_t(len(value))
            if ctypes.CDLL(None).sysctlbyname(
                b"kern.bootsessionuuid", value, ctypes.byref(size), None, ctypes.c_size_t(0)
            ) == 0:
                return value.value.decode("ascii") or None
        elif os.name == "nt":
            import winreg

            key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                return str(winreg.QueryValueEx(key, "BootId")[0])
        else:
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() or None
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError):
        pass
    return None


def _boot_time() -> float | None:
    """Wall-clock time of the last boot, counting sleep (macOS ``CLOCK_UPTIME_RAW``
    does not); it bounds the names an older build could have written."""
    try:
        clock = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
        return time.time() - time.clock_gettime(clock)
    except (AttributeError, OSError, ValueError):
        return None


def _legacy_machine_id() -> str:
    """The name 2.5.4 and older give this machine at this moment."""
    try:
        booted = round((time.time() - time.clock_gettime(time.CLOCK_UPTIME_RAW)) / 10)
    except (AttributeError, OSError, ValueError):
        booted = 0
    return hashlib.sha1(f"{socket.gethostname()}|{booted}".encode()).hexdigest()[:12]


_MACHINE_ID = _machine_id()
_BOOT_ID = _boot_id()
_BOOT_TIME = _boot_time()
# Builds up to 2.5.4 read only "machine". Writing their name for this
# process there keeps an older viewer still running here from taking a
# 2.5.5 lock for a foreign machine's and sweeping its live build.
_LEGACY_MACHINE_ID = _legacy_machine_id()
# NTP steps and slew move the computed boot time by seconds; a reboot moves
# it by at least the previous session's uptime.
_BOOT_TOLERANCE_S = 120.0
_FOREIGN_LOCK_STALE_S = 60.0
# 2.5.4 and older named a lock sha1("host|b")[:12], with b the boot time
# from CLOCK_UPTIME_RAW in tens of seconds, or 0 where that clock is missing
# (Windows, Linux), and wrote no boot time. On macOS that b grew by the time
# slept, so a viewer of that version still running here may have written any
# b from this boot up to now. Matching every candidate lets its pid decide,
# rather than treating its live build as a foreign machine's. Beyond this
# many candidates (about 115 days slept since boot) the oldest are skipped.
_LEGACY_ID_LIMIT = 1_000_000
_legacy_ids: dict[str, Any] = {"key": None, "high": None, "ids": set()}
_legacy_ids_lock = threading.Lock()


def _legacy_machine_ids() -> set[str]:
    host = socket.gethostname()

    def legacy(b: int) -> str:
        return hashlib.sha1(f"{host}|{b}".encode()).hexdigest()[:12]

    try:
        drifted = time.time() - time.clock_gettime(time.CLOCK_UPTIME_RAW)
    except (AttributeError, OSError, ValueError):
        return {legacy(0)}
    booted = _BOOT_TIME if _BOOT_TIME is not None else drifted
    high = round((drifted + _BOOT_TOLERANCE_S) / 10)
    low = max(round((booted - _BOOT_TOLERANCE_S) / 10), high - _LEGACY_ID_LIMIT)
    with _legacy_ids_lock:
        if _legacy_ids["key"] != (host, low) or _legacy_ids["high"] is None:
            _legacy_ids.update(key=(host, low), high=low - 1, ids=set())
        ids = _legacy_ids["ids"]
        ids.update(legacy(b) for b in range(_legacy_ids["high"] + 1, high + 1))
        _legacy_ids["high"] = max(_legacy_ids["high"], high)
        return ids


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
    if is_file_container(container):
        from .storage.single_file import newer_file_format

        newer_storage = newer_file_format(container)
        if newer_storage is not None:
            return f"nd2svs-sqlite/{newer_storage}"
    try:
        m = _manifest_data(Path(container))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(m, dict) or not m.get("complete"):
        return None
    number = _format_number(m.get("format"))
    newest_known = max(_format_number(f) or 0 for f in KNOWN_FORMATS)
    return str(m.get("format")) if number is not None and number > newest_known else None


def _plane_part(value: Any, words: tuple[str, ...] = ()) -> str:
    # None is what a store without that axis records (an SVS cache has
    # z_resolved None), and it has always been named "None" on disk.
    if value is None:
        return "None"
    if isinstance(value, numbers.Integral) and not isinstance(value, bool) and value >= 0:
        return str(int(value))
    if isinstance(value, str) and (value in words or re.fullmatch(r"[0-9]+", value)):
        return value
    raise ValueError(f"not a plane index: {value!r}")


def selection_tag(selection: Any) -> str:
    """``t0-p0-zmid`` — the requested selection, as named in the path.

    The selection can come from a manifest inside a cache file someone
    else made, so only plane indices and the z keywords may reach a path.
    """
    sel = selection.describe() if hasattr(selection, "describe") else dict(selection)
    t = _plane_part(sel.get("t", 0))
    p = _plane_part(sel.get("p", 0))
    z = _plane_part(sel.get("z", "mid"), ("mid", "max"))
    return f"t{t}-p{p}-z{z}"


def normalize_name(name: str) -> str:
    """Canonical NFC filename form for cross-OS macOS/Windows portability.

    Strictly uses NFC (never NFKC/NFKD) so fullwidth punctuation such as
    U+FF0F ('／') can never turn into path separators.
    """
    return unicodedata.normalize("NFC", name)


def names_match(a: str | None, b: str | None) -> bool:
    """True when two filenames are equal after NFC normalization."""
    return isinstance(a, str) and isinstance(b, str) and normalize_name(a) == normalize_name(b)


def resolve_existing_path(path: Path, *, kind: str = "any") -> Path:
    """Return ``path`` or an NFC/NFD-equivalent existing sibling on disk."""
    path = Path(path)
    if kind not in ("any", "file", "dir"):
        raise ValueError(f"invalid kind: {kind!r}")
    fs_path = Path(filesystem_path(path))
    if fs_path.exists() or fs_path.is_symlink():
        return path
    if path.name.isascii():
        return path
    fs_parent = Path(filesystem_path(path.parent))
    if not fs_parent.is_dir():
        return path
    norm = normalize_name(path.name)
    matches: list[str] = []
    try:
        with os.scandir(fs_parent) as entries:
            for entry in entries:
                if normalize_name(entry.name) != norm:
                    continue
                try:
                    if kind == "file" and entry.is_dir(follow_symlinks=False):
                        continue
                    if kind == "dir" and not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                matches.append(entry.name)
    except OSError:
        return path
    if matches:
        return path.parent / min(matches)
    return path


def managed_dir(slide: str | Path) -> Path:
    return Path(slide).parent / MANAGED_DIR


def source_tag(slide: str | Path) -> str:
    """Filesystem-safe source name, including its extension.

    Keeping ``.nd2`` or ``.svs`` in the key prevents two files with the same
    stem from sharing a cache container.
    """
    return re.sub(r'[\/:*?"<>|\x00-\x1f]+', "_", normalize_name(Path(slide).name))


def legacy_cache_container(
    slide: str | Path, selection: Any = None, cache_dir: str | Path | None = None
) -> Path:
    """The stem-only managed path written by versions 0.8 and 0.9."""
    from .reader import PlaneSelection

    slide = Path(slide)
    base = Path(cache_dir) if cache_dir else managed_dir(slide) / CACHES_DIR
    tag = selection_tag(selection or PlaneSelection())
    return base / f"{normalize_name(slide.stem)}--{tag}{CACHE_SUFFIX}"


def directory_cache_container(
    slide: str | Path, selection: Any = None, cache_dir: str | Path | None = None
) -> Path:
    """The v1 cache path, keyed by source filename and selected plane."""
    from .reader import PlaneSelection

    slide = Path(slide)
    base = Path(cache_dir) if cache_dir else managed_dir(slide) / CACHES_DIR
    tag = selection_tag(selection or PlaneSelection())
    return base / f"{source_tag(slide)}--{tag}{CACHE_SUFFIX}"


def cache_container(
    slide: str | Path, selection: Any = None, cache_dir: str | Path | None = None
) -> Path:
    """The v2 single-file cache, keyed by source filename and selected plane."""
    old = directory_cache_container(slide, selection, cache_dir)
    return old.with_name(old.name[:-len(CACHE_SUFFIX)] + SINGLE_FILE_SUFFIX)


def is_file_container(path: str | Path) -> bool:
    """Include our staging siblings; existence is not required during a build."""
    if Path(path).is_dir():
        return False
    return Path(path).name.lower().endswith(SINGLE_FILE_SUFFIX) or (
        SINGLE_FILE_SUFFIX + ".building-" in Path(path).name.lower()
    )


def is_managed_cache_path(
    path: Path | None, manifest: dict[str, Any] | None = None
) -> bool:
    """True when ``path`` sits in an auto-managed ``nd2wsi/caches`` folder."""
    if path is None or (manifest or {}).get("origin") == "export":
        return False
    return path.parent.name == CACHES_DIR and path.parent.parent.name == MANAGED_DIR


def container_store(container: str | Path) -> Path:
    return Path(container) if is_file_container(container) else Path(container) / STORE_NAME


def manifest_container(store: str | Path) -> Path | None:
    store = Path(store)
    if is_file_container(store):
        return store
    if store.name.endswith(CACHE_SUFFIX):
        return store
    return store.parent if store.parent.name.endswith(CACHE_SUFFIX) else None


def source_base(container: str | Path) -> Path:
    container = Path(container)
    return container.parent if is_file_container(container) else container


def _manifest_data(container: Path, *, recover: bool = False) -> Any:
    if is_file_container(container):
        from .storage.single_file import read_entry

        return json.loads(read_entry(container, MANIFEST_NAME, recover=recover))
    with open(filesystem_path(container / MANIFEST_NAME), encoding="utf-8") as fh:
        return json.loads(fh.read())


def quick_fingerprint(path: str | Path) -> dict[str, Any]:
    """Identity of a source file, cheap enough to take on every open.

    Size, mtime, and a hash over the head, middle and tail megabyte. This
    is a stale-cache detector, not a full content hash: a change that
    leaves size, mtime and all three sampled windows intact goes unseen.
    """
    path = Path(path)
    native = filesystem_path(path)
    st = os.stat(native)
    h = hashlib.sha256()
    with open(native, "rb") as fh:
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


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_quantized_pair(a_ns: int, b_ns: int, quantum_ns: int) -> bool:
    a_aligned = a_ns % quantum_ns == 0
    b_aligned = b_ns % quantum_ns == 0
    if a_aligned == b_aligned:
        return False
    coarse, fine = (a_ns, b_ns) if a_aligned else (b_ns, a_ns)
    floored = (fine // quantum_ns) * quantum_ns
    return coarse in (floored, floored + quantum_ns)


def _exfat_mtime_skew_matches(a_ns: int, b_ns: int) -> bool:
    if a_ns <= 0 or b_ns <= 0:
        return False
    delta = b_ns - a_ns
    q, r = divmod(abs(delta), _EXFAT_TZ_STEP_NS)
    steps = q + (1 if r * 2 > _EXFAT_TZ_STEP_NS else 0)
    if steps > _EXFAT_MAX_TZ_STEPS:
        return False
    signed_steps = steps if delta >= 0 else -steps
    b_adj = b_ns - signed_steps * _EXFAT_TZ_STEP_NS
    rem = abs(b_adj - a_ns)
    if rem == 0:
        return True
    if rem <= _EXFAT_10MS_NS and (
        _is_quantized_pair(a_ns, b_adj, _EXFAT_10MS_NS)
        or (a_ns % _EXFAT_10MS_NS == 0 and b_adj % _EXFAT_10MS_NS == 0)
    ):
        return True
    if rem < _COARSE_1S_NS and _is_quantized_pair(a_ns, b_adj, _COARSE_1S_NS):
        return True
    if rem < _FAT_2S_NS and _is_quantized_pair(a_ns, b_adj, _FAT_2S_NS):
        fine = a_ns if b_adj % _FAT_2S_NS == 0 else b_adj
        return signed_steps != 0 or fine % _COARSE_1S_NS != 0
    return False


def fingerprints_match(
    a: dict[str, Any], b: dict[str, Any], *, allow_mtime_skew: bool = True
) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    size_a, size_b = a.get("size"), b.get("size")
    sha_a, sha_b = a.get("quick_sha256"), b.get("quick_sha256")
    mtime_a, mtime_b = a.get("mtime_ns"), b.get("mtime_ns")
    if (
        size_a is None
        or size_b is None
        or sha_a is None
        or sha_b is None
        or mtime_a is None
        or mtime_b is None
    ):
        return False
    if any(
        isinstance(v, bool)
        for v in (size_a, size_b, sha_a, sha_b, mtime_a, mtime_b)
    ):
        return False
    if size_a != size_b or sha_a != sha_b:
        return False
    if mtime_a == mtime_b:
        return True
    if not allow_mtime_skew:
        return False
    if not (
        _is_int(size_a)
        and _is_int(size_b)
        and isinstance(sha_a, str)
        and bool(sha_a)
        and _is_int(mtime_a)
        and _is_int(mtime_b)
    ):
        return False
    return _exfat_mtime_skew_matches(mtime_a, mtime_b)


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
    origin: str | None = None,
) -> None:
    from . import __version__

    try:
        rel_path = Path(os.path.relpath(slide, source_base(container))).as_posix()
    except ValueError:
        rel_path = ""
    manifest: dict[str, Any] = {
        "format": SINGLE_FILE_FORMAT if is_file_container(container) else (
            MANIFEST_FORMAT if kind == "full" else OVERVIEW_FORMAT
        ),
        "kind": kind,
        "complete": True,
        "generation": uuid.uuid4().hex,
        "source": {
            **fingerprint,
            "relative_path": rel_path,
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
    if origin is not None:
        manifest["origin"] = origin
    if is_file_container(container):
        from .storage.single_file import write_entry

        manifest["storage"].update({"container": "nd2svs-sqlite/2"})
        write_entry(container, MANIFEST_NAME, json.dumps(manifest, indent=1).encode())
        return
    tmp = container / f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
    tmp_native = filesystem_path(tmp)
    try:
        with open(tmp_native, "wb") as fh:
            fh.write(json.dumps(manifest, indent=1).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_native, filesystem_path(container / MANIFEST_NAME))
    finally:
        Path(tmp_native).unlink(missing_ok=True)


def read_manifest(container: str | Path, *, recover: bool = False) -> dict[str, Any] | None:
    """Read metadata; only authorized cache-open paths opt into crash recovery."""
    try:
        m = _manifest_data(Path(container), recover=recover)
    except (OSError, UnicodeError, ValueError):
        return None
    return m if isinstance(m, dict) and m.get("complete") else None


def cache_matches(
    container: Path,
    slide: Path,
    selection: Any,
    kind: str | None = None,
    tile: int | None = None,
) -> bool:
    """A cache is valid for this slide and selection, or it is stale.

    ``kind`` narrows the match ("full" or "overview"); None takes either.
    """
    m = read_manifest(container)
    if m is None or m.get("format") not in KNOWN_FORMATS:
        return False
    if m.get("pyramid", {}).get("algorithm") != ALGORITHM:
        return False
    if tile is not None and int((m.get("pyramid") or {}).get("tile") or 0) != int(tile):
        return False
    if kind is not None and m.get("kind", "full") != kind:
        return False
    source_info = m.get("source")
    if not isinstance(source_info, dict):
        return False
    stored_name = source_info.get("name")
    if stored_name is not None and not names_match(stored_name, slide.name):
        return False
    sel = selection.describe() if hasattr(selection, "describe") else dict(selection)
    stored = m.get("selection", {})
    if any(str(stored.get(k)) != str(v) for k, v in sel.items()):
        return False
    try:
        return fingerprints_match(source_info, quick_fingerprint(slide))
    except OSError:
        return False


def _refuse_database_companions(path: Path) -> None:
    """Never detach a database from a possible recovery journal."""
    for suffix in ('-journal', '-wal', '-shm'):
        companion = path.with_name(path.name + suffix)
        if os.path.lexists(filesystem_path(companion)):
            raise RuntimeError(f"cache has a database journal; preserve and recover it before replacement: {companion}")


def quarantine(path: Path) -> Path:
    """Set a broken artifact aside instead of destroying it."""
    _refuse_database_companions(path)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 0
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
    try:
        path.rename(target)
    except PermissionError as exc:
        if getattr(exc, 'winerror', None) in (32, 33):
            raise PermissionError(
                f"This cache is still open in a viewer. Save annotations, close all windows "
                f"using {path.name}, and reopen the source to rebuild it. The cache was preserved."
            ) from exc
        raise
    return target


def _taken_elsewhere(info: dict[str, Any]) -> bool:
    """True when a lock was taken on another machine or in another boot."""
    if "host" in info:  # written by 2.5.5 or later
        if info.get("host") != _MACHINE_ID:
            return True
        boot = info.get("boot")
        return boot is not None and _BOOT_ID is not None and boot != _BOOT_ID
    # 2.5.4 and older wrote only their own name of host and boot
    machine = info.get("machine")
    return machine is not None and machine not in _legacy_machine_ids()


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
        container = Path(container)
        canonical = container.with_name(normalize_name(container.name) + ".lock")
        resolved_lock = resolve_existing_path(canonical, kind="file")
        resolved_arbiter = resolve_existing_path(
            resolved_lock.with_name(resolved_lock.name + ".arbiter"),
            kind="file",
        )
        if (
            resolved_lock == canonical
            and resolved_arbiter.name != canonical.name + ".arbiter"
            and resolved_arbiter.name.endswith(".arbiter")
        ):
            fs_resolved = Path(filesystem_path(resolved_lock))
            if not (fs_resolved.exists() or fs_resolved.is_symlink()):
                resolved_lock = resolved_arbiter.with_name(
                    resolved_arbiter.name[: -len(".arbiter")]
                )
        self.path = resolved_lock
        self._gen: str | None = None
        self._fd: int | None = None
        self._arbiter = SessionFileLock(resolved_arbiter)
        self._mutex = threading.RLock()

    def _read(self, path: Path | None = None) -> dict[str, Any] | None:
        try:
            with open(filesystem_path(path or self.path), encoding="utf-8") as fh:
                info = json.loads(fh.read())
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return info if isinstance(info, dict) else None

    @staticmethod
    def _read_fd(fd: int) -> tuple[dict[str, Any] | None, os.stat_result]:
        st = os.fstat(fd)
        try:
            info = json.loads(read_at(fd, st.st_size, 0))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            info = None
        return (info if isinstance(info, dict) else None), st

    @staticmethod
    def _write_fd(fd: int, payload: bytes) -> None:
        """Replace one opened lock inode's small payload without path lookup."""
        view = memoryview(payload)
        offset = 0
        while view:
            written = write_at(fd, view, offset)
            if written <= 0:
                raise OSError("short write while updating cache lock")
            offset += written
            view = view[written:]
        os.ftruncate(fd, offset)

    def _path_is_fd(self, fd: int) -> bool:
        try:
            held = os.fstat(fd)
            current = os.stat(filesystem_path(self.path), follow_symlinks=False)
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
            "machine": _LEGACY_MACHINE_ID,
            "host": _MACHINE_ID,
            "boot": _BOOT_ID,
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
            set_fd_times(fd, 0, 0)
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
                and info.get("host") == _MACHINE_ID
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
        if _taken_elsewhere(info):
            # taken on another machine or before a reboot: its process
            # cannot be running here, and only clock skew argues for a
            # short grace
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
            return not process_is_alive(pid)
        except (ProcessLookupError, TypeError, ValueError, OverflowError):
            return True
        except PermissionError:
            return False

    def acquire(self, timeout: float = 3600.0, poll: float = 0.5) -> None:
        timeout = max(0.0, float(timeout))
        poll = max(0.001, float(poll))
        deadline = time.monotonic() + timeout
        with self._mutex:
            if self._fd is not None:
                return
            native = filesystem_path(self.path)
            os.makedirs(filesystem_path(self.path.parent), exist_ok=True)
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
                            native,
                            os.O_CREAT | os.O_EXCL | os.O_RDWR | BINARY,
                        )
                        acquired = self._install_claim(fd, payload, gen)
                    except FileExistsError:
                        try:
                            pre_st = os.lstat(native)
                            if (
                                stat.S_ISLNK(pre_st.st_mode)
                                or is_windows_name_surrogate_reparse(pre_st)
                            ):
                                fd = None
                            else:
                                fd = os.open(
                                    native,
                                    os.O_RDWR | BINARY | getattr(os, "O_NOFOLLOW", 0),
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
                            if (
                                self._safe_lock_inode(st)
                                and not is_windows_name_surrogate_reparse(st, fd=fd)
                                and self._path_is_fd(fd)
                                and self._stale_info(info, st.st_mtime)
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
                            set_fd_times(fd, 0, 0)
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

    Files use one atomic replacement. A previous directory is set aside
    first and removed after the new one is in place; a failure puts it back.
    """
    _refuse_database_companions(staging)
    _refuse_database_companions(final)
    staging_native = filesystem_path(staging)
    final_native = filesystem_path(final)
    staging_st = os.lstat(staging_native)
    if stat.S_ISLNK(staging_st.st_mode) or is_windows_name_surrogate_reparse(staging_st):
        raise ValueError("cache publication refuses symbolic links and junctions")
    try:
        final_st = os.lstat(final_native)
    except FileNotFoundError:
        final_st = None
    if final_st is not None and (
        stat.S_ISLNK(final_st.st_mode) or is_windows_name_surrogate_reparse(final_st)
    ):
        raise ValueError("cache publication refuses symbolic links and junctions")
    if stat.S_ISREG(staging_st.st_mode) and (final_st is None or stat.S_ISREG(final_st.st_mode)):
        # A single replacement keeps the pathname continuously valid. Existing
        # POSIX readers retain their old inode; Windows refuses if a reader has
        # not released its handle. Either outcome preserves a complete cache.
        os.replace(staging_native, final_native)
        return
    backup = None
    backup_native = None
    if final_st is not None:
        backup = final.with_name(final.name + f".replaced-{uuid.uuid4().hex[:8]}")
        backup_native = filesystem_path(backup)
        os.rename(final_native, backup_native)
    try:
        os.rename(staging_native, final_native)
    except OSError:
        if backup_native is not None:
            os.rename(backup_native, final_native)
        raise
    if backup is not None:
        _remove_artifact(backup)


def _remove_artifact(path: Path) -> None:
    """Remove one exact app-owned staging artifact, whether file or directory."""
    _refuse_database_companions(path)
    native = filesystem_path(path)
    try:
        st = os.lstat(native)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or stat.S_ISREG(st.st_mode):
        try:
            os.unlink(native)
        except FileNotFoundError:
            pass
        return
    if is_windows_name_surrogate_reparse(st):
        raise ValueError(f"refusing to recursively remove junction or reparse point: {path}")
    shutil.rmtree(native)


def _sweep_lock_candidates(caches_dir: Path, family: str) -> list[CacheLock]:
    """Return raw, NFC-normalized, and sibling-resolved lock candidates for ``family``."""
    raw_container = caches_dir / family
    nfc_container = caches_dir / normalize_name(family)
    candidates: list[CacheLock] = []
    seen_paths: set[Path] = set()
    for candidate_container in (raw_container, nfc_container):
        resolved = CacheLock(candidate_container)
        if resolved.path not in seen_paths:
            seen_paths.add(resolved.path)
            candidates.append(resolved)
        exact_path = candidate_container.with_name(candidate_container.name + ".lock")
        if exact_path not in seen_paths:
            seen_paths.add(exact_path)
            exact_lock = CacheLock.__new__(CacheLock)
            exact_lock.path = exact_path
            exact_lock._gen = None
            exact_lock._fd = None
            exact_lock._arbiter = SessionFileLock(
                exact_path.with_name(exact_path.name + ".arbiter")
            )
            exact_lock._mutex = threading.RLock()
            candidates.append(exact_lock)
    candidates.sort(key=lambda item: str(item._arbiter.path))
    return candidates


def sweep_stale_builds(caches_dir: Path) -> int:
    """Remove leftover staging and backup dirs whose builder is gone.

    Covers ``*.building-<pid>`` staging and ``*.replaced-*`` backups that
    a crash inside commit_container can strand. Liveness is judged from
    the family's exact lockfile — names are never fed to glob, so a
    bracketed NIS-Elements filename cannot inject wildcards.
    """
    n = 0
    caches_native = filesystem_path(caches_dir)
    if not os.path.isdir(caches_native):
        return 0
    with os.scandir(caches_native) as it:
        names = [entry.name for entry in it]
    for name in names:
        item = caches_dir / name
        match = re.fullmatch(r"(.+\.(?:nd2wsi-cache|nd2svs))\.(?:building|replaced)-[^/]+", name)
        if match is None:
            continue
        family = match.group(1)
        locks = _sweep_lock_candidates(caches_dir, family)
        held_arbiters: list[SessionFileLock] = []
        held_inodes: set[tuple[int, int]] = set()
        held_paths: set[Path] = set()
        contended = False
        try:
            for lock in locks:
                arb_native = filesystem_path(lock._arbiter.path)
                try:
                    arb_st = os.lstat(arb_native)
                except FileNotFoundError:
                    continue
                except OSError:
                    contended = True
                    break
                arb_ino = (arb_st.st_dev, arb_st.st_ino)
                if arb_ino != (0, 0) and arb_ino in held_inodes:
                    held_paths.add(lock._arbiter.path)
                    continue
                try:
                    lock._arbiter.acquire(timeout=0)
                    held_arbiters.append(lock._arbiter)
                    held_paths.add(lock._arbiter.path)
                    if arb_ino != (0, 0):
                        held_inodes.add(arb_ino)
                except (TimeoutError, OSError):
                    contended = True
                    break
            if contended:
                continue  # another local process is actively coordinating this family
            live = False
            unreadable = False
            for lock in locks:
                try:
                    lock_st = os.lstat(filesystem_path(lock.path))
                except FileNotFoundError:
                    continue
                except OSError:
                    unreadable = True
                    break  # unreadable lockfile: assume build may still be active
                info = lock._read()
                if info is None:
                    # Momentarily empty/unreadable while being written: honor 30s grace.
                    if not CacheLock._stale_info(None, lock_st.st_mtime):
                        live = True
                        break
                elif info.get("released") is True:
                    continue
                elif _taken_elsewhere(info):
                    continue  # a builder on another machine or boot is not running here
                else:
                    try:
                        if process_is_alive(int(info.get("pid", -1))):
                            live = True
                            break
                    except (ProcessLookupError, ValueError, TypeError, OverflowError):
                        pass
                    except PermissionError:
                        live = True
                        break
            if unreadable or live:
                continue
            try:
                _refuse_database_companions(item)
                item_st = os.lstat(filesystem_path(item))
                if stat.S_ISREG(item_st.st_mode):
                    from .storage.single_file import is_single_file

                    if not is_single_file(item):
                        continue  # an arbitrary file with a staging-like name is not cache
                elif (
                    stat.S_ISDIR(item_st.st_mode)
                    and not stat.S_ISLNK(item_st.st_mode)
                    and not is_windows_name_surrogate_reparse(item_st)
                    and ".replaced-" in name
                ):
                    from .cache_removal import rescue_annotations

                    rescue_annotations(item, caches_dir.parent / ANNOTATIONS_DIR)
                if any(
                    lock._arbiter.path not in held_paths
                    and os.path.exists(filesystem_path(lock._arbiter.path))
                    for lock in locks
                ):
                    continue  # a local builder started while we inspected this family
                _remove_artifact(item)
                n += 1
            except (OSError, RuntimeError, ValueError):
                pass  # leave inaccessible or unrescuable artifacts in place
        finally:
            for arbiter in reversed(held_arbiters):
                arbiter.release()
    return n
