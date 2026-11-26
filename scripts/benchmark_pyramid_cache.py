#!/usr/bin/env python3
"""Cross-platform stage-by-stage pyramid caching benchmark and release-gate harness.

Measures end-to-end verified ``.nd2svs`` conversion time, stage breakdowns
(first level, later levels, OMERO windows, reopened-database SHA-256
verification, and finalize/commit), final cache size, temp disk peak, peak RSS,
viewport tile-read latency (p50/p95 across Levels 1–3), and decoded pixel
SHA-256 hashes at every stored level.

Isolation guarantees:
- Never writes into or modifies source slide directories.
- Creates a unique per-run child directory inside ``--out-dir`` and deletes only
  that child directory in ``finally`` (never deletes ``--out-dir`` itself).
- Runs each repeat in an isolated child Python subprocess for clean kernel
  ``ru_maxrss`` / Windows ``PeakWorkingSetSize`` accounting.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # pragma: no cover - Windows lacks POSIX resource
    resource = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Host, Filesystem, RSS & Recursive Temp-Disk Samplers (stdlib + ctypes only)
# ---------------------------------------------------------------------------


class _WinProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _WinMemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _win32_process_rss_mib() -> tuple[float, float]:
    """Return ``(current_working_set_mib, peak_working_set_mib)`` on Windows."""
    if os.name != "nt":
        return 0.0, 0.0
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        get_proc = kernel32.GetCurrentProcess
        get_proc.argtypes = []
        get_proc.restype = ctypes.c_void_p
        fn = getattr(kernel32, "K32GetProcessMemoryInfo", None)
        if fn is None:
            psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
            fn = psapi.GetProcessMemoryInfo
        fn.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_WinProcessMemoryCounters),
            ctypes.c_uint32,
        ]
        fn.restype = ctypes.c_int
        pmc = _WinProcessMemoryCounters()
        pmc.cb = ctypes.sizeof(_WinProcessMemoryCounters)
        if fn(get_proc(), ctypes.byref(pmc), pmc.cb):
            mib = 1024.0 * 1024.0
            return float(pmc.WorkingSetSize) / mib, float(pmc.PeakWorkingSetSize) / mib
    except Exception:
        pass
    return 0.0, 0.0


def current_ru_maxrss_mib() -> float:
    if resource is not None:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return float(ru) / (1024.0 * 1024.0)
        return float(ru) / 1024.0
    if os.name == "nt":
        _, peak_mib = _win32_process_rss_mib()
        return peak_mib
    return 0.0


class _DarwinProcTaskInfo(ctypes.Structure):
    _fields_ = [
        ("pti_virtual_size", ctypes.c_uint64),
        ("pti_resident_size", ctypes.c_uint64),
        ("pti_total_user", ctypes.c_uint64),
        ("pti_total_system", ctypes.c_uint64),
        ("pti_threads_user", ctypes.c_uint64),
        ("pti_threads_system", ctypes.c_uint64),
        ("pti_policy", ctypes.c_int32),
        ("pti_faults", ctypes.c_int32),
        ("pti_pageins", ctypes.c_int32),
        ("pti_cow_faults", ctypes.c_int32),
        ("pti_messages_sent", ctypes.c_int32),
        ("pti_messages_received", ctypes.c_int32),
        ("pti_syscalls_mach", ctypes.c_int32),
        ("pti_syscalls_unix", ctypes.c_int32),
        ("pti_csw", ctypes.c_int32),
        ("pti_threadnum", ctypes.c_int32),
        ("pti_numrunning", ctypes.c_int32),
        ("pti_priority", ctypes.c_int32),
    ]


_DARWIN_PROC_PIDINFO: Any = None
_DARWIN_PROC_PIDINFO_INIT = False


def _darwin_process_rss_mib(pid: int) -> float:
    global _DARWIN_PROC_PIDINFO, _DARWIN_PROC_PIDINFO_INIT
    if sys.platform != "darwin":
        return 0.0
    try:
        if not _DARWIN_PROC_PIDINFO_INIT:
            _DARWIN_PROC_PIDINFO_INIT = True
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            fn = getattr(libc, "proc_pidinfo", None)
            if fn is not None:
                fn.argtypes = [
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_uint64,
                    ctypes.c_void_p,
                    ctypes.c_int,
                ]
                fn.restype = ctypes.c_int
                _DARWIN_PROC_PIDINFO = fn
        if _DARWIN_PROC_PIDINFO is not None:
            info = _DarwinProcTaskInfo()
            sz = ctypes.sizeof(info)
            if _DARWIN_PROC_PIDINFO(int(pid), 4, 0, ctypes.byref(info), sz) == sz:
                return float(info.pti_resident_size) / (1024.0 * 1024.0)
    except Exception:
        pass
    return 0.0


def _sample_live_rss_mib(pid: int) -> float:
    if os.name == "nt":
        if pid == os.getpid():
            cur_mib, _ = _win32_process_rss_mib()
            return cur_mib
        return 0.0
    if sys.platform == "darwin":
        mib = _darwin_process_rss_mib(pid)
        if mib > 0.0:
            return mib
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return float(int(out)) / 1024.0
    except Exception:
        pass
    return 0.0


class RssSampler:
    """Samples live process RSS alongside kernel ``ru_maxrss`` / peak working set."""

    def __init__(self, interval_s: float = 0.025) -> None:
        self.pid = os.getpid()
        self.interval_s = interval_s
        self.peak_live_mib = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="rss-sampler", daemon=True)

    def _sample_once(self) -> None:
        mib = _sample_live_rss_mib(self.pid)
        if mib > self.peak_live_mib:
            self.peak_live_mib = mib

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(self.interval_s)

    def __enter__(self) -> RssSampler:
        self._sample_once()
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._sample_once()

    def report(self) -> dict[str, float]:
        ru_mib = current_ru_maxrss_mib()
        return {
            "ru_maxrss_mib": round(ru_mib, 1),
            "live_peak_rss_mib": round(self.peak_live_mib, 1),
            "peak_rss_mib": round(max(ru_mib, self.peak_live_mib), 1),
        }


def _recursive_dir_bytes(root: Path) -> int:
    """Sum file sizes under ``root`` via ``os.scandir``, ignoring transient errors."""
    total = 0
    stack = [str(root)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


class TempDiskSampler:
    """Background recursive byte-sum sampler for transient staging disk peak."""

    def __init__(self, target_dir: Path, interval_s: float = 0.05) -> None:
        self.target_dir = Path(target_dir)
        self.interval_s = interval_s
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="disk-sampler", daemon=True)

    def _sample_once(self) -> None:
        cur = _recursive_dir_bytes(self.target_dir)
        if cur > self.peak_bytes:
            self.peak_bytes = cur

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(self.interval_s)

    def __enter__(self) -> TempDiskSampler:
        self._sample_once()
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._sample_once()


def _total_ram_gib() -> float | None:
    try:
        if sys.platform == "darwin":
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
            return round(int(out) / (1024.0**3), 2)
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            stat = _WinMemoryStatusEx()
            stat.dwLength = ctypes.sizeof(stat)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return round(float(stat.ullTotalPhys) / (1024.0**3), 2)
        if Path("/proc/meminfo").is_file():
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    kib = int(line.split()[1])
                    return round(kib / (1024.0**2), 2)
    except Exception:
        pass
    return None


def _cpu_model() -> str:
    try:
        if sys.platform == "darwin":
            out = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            if out:
                return out
        if Path("/proc/cpuinfo").is_file():
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or platform.machine()


def _filesystem_info(path: Path) -> dict[str, Any]:
    """Identify filesystem type and allocation block size for ``path``."""
    target = path if path.exists() else path.parent
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target

    fs_type = "unknown"
    mount_point = ""
    alloc_block: int | None = None

    if os.name == "nt":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            drive = resolved.drive
            root_path = (drive + "\\") if drive else str(resolved)
            fs_buf = ctypes.create_unicode_buffer(261)
            vol_buf = ctypes.create_unicode_buffer(261)
            if kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(root_path),
                vol_buf,
                len(vol_buf),
                None,
                None,
                None,
                fs_buf,
                len(fs_buf),
            ):
                fs_type = fs_buf.value or "unknown"
            mount_point = root_path
            spc = ctypes.c_uint32()
            bps = ctypes.c_uint32()
            nfc = ctypes.c_uint32()
            tnc = ctypes.c_uint32()
            if kernel32.GetDiskFreeSpaceW(
                ctypes.c_wchar_p(root_path),
                ctypes.byref(spc),
                ctypes.byref(bps),
                ctypes.byref(nfc),
                ctypes.byref(tnc),
            ):
                alloc_block = int(spc.value) * int(bps.value)
        except Exception:
            pass
        return {"fs_type": fs_type, "mount": mount_point, "alloc_block_bytes": alloc_block}

    try:
        stv = os.statvfs(str(resolved))
        alloc_block = int(stv.f_bsize or stv.f_frsize)
    except OSError:
        pass

    resolved_str = str(resolved)
    best_len = -1
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["mount"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if " on " not in line or " (" not in line:
                    continue
                _, rest = line.split(" on ", 1)
                mp, opts = rest.rsplit(" (", 1)
                fst = opts.split(",", 1)[0].rstrip(")").strip()
                if resolved_str == mp or resolved_str.startswith(mp.rstrip("/") + "/"):
                    if len(mp) > best_len:
                        best_len = len(mp)
                        mount_point = mp
                        fs_type = fst
        except Exception:
            pass
    elif Path("/proc/mounts").is_file():
        try:
            for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1].replace("\\040", " ")
                fst = parts[2]
                if resolved_str == mp or resolved_str.startswith(mp.rstrip("/") + "/"):
                    if len(mp) > best_len:
                        best_len = len(mp)
                        mount_point = mp
                        fs_type = fst
        except Exception:
            pass

    return {"fs_type": fs_type, "mount": mount_point, "alloc_block_bytes": alloc_block}


def _git_head(repo_dir: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return None


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (pct / 100.0) * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    frac = pos - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


# ---------------------------------------------------------------------------
# Child Subprocess Execution (Zero-Pollution Stage Instrumentation)
# ---------------------------------------------------------------------------


def _hash_level_pixels(arr: Any, tile: int) -> str:
    """Stream decoded pixels row-band-by-row-band to SHA-256 without blowing RSS."""
    import numpy as np

    c, h, w = (int(v) for v in arr.shape)
    hsh = hashlib.sha256()
    step = max(tile, 512)
    for ci in range(c):
        for y0 in range(0, h, step):
            y1 = min(h, y0 + step)
            band = np.ascontiguousarray(np.asarray(arr[ci, y0:y1, :]))
            hsh.update(band.tobytes())
    return hsh.hexdigest()


def _measure_viewport_reads(out_path: Path, *, seed: int = 2612, n_reads: int = 32) -> dict[str, Any]:
    """Measure 32 fixed-seed center & edge tile reads across Levels 1–3 via ``open_store``."""
    import numpy as np

    from nd2wsi.convert import open_store

    rng = random.Random(seed)
    root, attrs = open_store(out_path)
    try:
        tile = int(attrs["nd2wsi"]["tile"])
        all_levels = [str(lv["path"]) for lv in attrs["nd2wsi"]["levels"]]
        target_levels = [lv for lv in ("1", "2", "3") if lv in all_levels]
        if not target_levels:
            target_levels = all_levels[:3]

        samples_ms: list[float] = []
        for idx in range(n_reads):
            lv_key = target_levels[idx % len(target_levels)]
            arr = root[lv_key]
            _, h, w = (int(v) for v in arr.shape)
            max_y = max(0, h - tile)
            max_x = max(0, w - tile)
            if idx % 2 == 0:
                # Center-biased tile
                cy = max(0, (h // 2) - (tile // 2))
                cx = max(0, (w // 2) - (tile // 2))
                jitter_y = min(tile, max_y)
                jitter_x = min(tile, max_x)
                y0 = max(0, min(max_y, cy + rng.randint(-jitter_y, jitter_y)))
                x0 = max(0, min(max_x, cx + rng.randint(-jitter_x, jitter_x)))
            else:
                # Edge tile
                edge = idx % 4
                if edge == 1:
                    y0 = 0
                    x0 = rng.randint(0, max_x) if max_x else 0
                else:
                    y0 = max_y
                    x0 = rng.randint(0, max_x) if max_x else 0
            y1 = min(h, y0 + tile)
            x1 = min(w, x0 + tile)
            t0 = time.perf_counter()
            _ = np.asarray(arr[:, y0:y1, x0:x1])
            samples_ms.append((time.perf_counter() - t0) * 1000.0)

        level_hashes = {lv: _hash_level_pixels(root[lv], tile) for lv in all_levels}
        omero_windows = [ch.get("window") for ch in attrs.get("omero", {}).get("channels", [])]
        return {
            "tile_actual": tile,
            "levels": all_levels,
            "viewport_reads_n": len(samples_ms),
            "viewport_p50_ms": round(float(statistics.median(samples_ms)), 3),
            "viewport_p95_ms": round(_percentile(samples_ms, 95.0), 3),
            "level_pixel_sha256": level_hashes,
            "omero_windows": omero_windows,
        }
    finally:
        close_fn = getattr(root, "close", None)
        if close_fn is not None:
            close_fn()


def _run_worker(spec: dict[str, Any]) -> dict[str, Any]:
    baseline_repo = spec.get("baseline_repo")
    if baseline_repo:
        repo_path = str(Path(baseline_repo).resolve())
        sys.path.insert(0, repo_path)

    import numcodecs.blosc as blosc

    from nd2wsi import cache as cache_mod
    from nd2wsi import convert as convert_mod
    from nd2wsi import convert_cache as cc
    from nd2wsi import migration as migration_mod
    from nd2wsi.storage.single_file import SQLiteStore
    from nd2wsi.storage.zarr_v2 import ZarrV2Storage

    slide = Path(spec["slide"])
    run_dir = Path(spec["run_dir"])
    out_path = run_dir / f"{slide.stem}.bench.nd2svs"
    mode = str(spec["mode"])
    overview = mode == "overview"
    arm = str(spec["arm"])
    tile_arg = spec["tile"]
    workers_arg = spec["workers"]
    tile_val: int | None = None if tile_arg == "auto" else int(tile_arg)
    workers_val: int | None = None if workers_arg == "auto" else int(workers_arg)

    stage: dict[str, Any] = {
        "first_level_s": 0.0,
        "later_levels_s": 0.0,
        "later_levels_per_level_s": [],
        "omero_windows_s": 0.0,
        "reopen_verify_s": 0.0,
        "finalize_commit_s": 0.0,
        "used_seq_reader": False,
    }
    in_verify_phase = {"active": False, "first_ro_timed": False}

    # Wrap stage 1: first_level
    real_write_l0 = cc._write_level0
    real_write_l1_dask = cc._write_level1_from_source

    def timed_l0(*a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return real_write_l0(*a, **kw)
        finally:
            stage["first_level_s"] += time.perf_counter() - t0

    def timed_l1_dask(*a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return real_write_l1_dask(*a, **kw)
        finally:
            stage["first_level_s"] += time.perf_counter() - t0

    cc._write_level0 = timed_l0  # type: ignore[assignment]
    cc._write_level1_from_source = timed_l1_dask  # type: ignore[assignment]

    if hasattr(cc, "_write_level1_sequential_nd2"):
        real_write_l1_seq = cc._write_level1_sequential_nd2

        def timed_l1_seq(*a: Any, **kw: Any) -> bool:
            t0 = time.perf_counter()
            ok = real_write_l1_seq(*a, **kw)
            stage["first_level_s"] += time.perf_counter() - t0
            if ok:
                stage["used_seq_reader"] = True
            return ok

        cc._write_level1_sequential_nd2 = timed_l1_seq  # type: ignore[assignment]

    if arm == "direct_only" and hasattr(cc, "sequential_nd2_layout"):
        cc.sequential_nd2_layout = lambda *a, **kw: None  # type: ignore[assignment]

    # Wrap stage 2: later stored-parent levels
    real_downsample = convert_mod._downsample_into

    def timed_downsample(*a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return real_downsample(*a, **kw)
        finally:
            dt = time.perf_counter() - t0
            stage["later_levels_s"] += dt
            stage["later_levels_per_level_s"].append(round(dt, 4))

    convert_mod._downsample_into = timed_downsample  # type: ignore[assignment]

    # Wrap stage 3: OMERO windows
    real_windows = convert_mod._percentile_windows

    def timed_windows(*a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return real_windows(*a, **kw)
        finally:
            stage["omero_windows_s"] += time.perf_counter() - t0

    convert_mod._percentile_windows = timed_windows  # type: ignore[assignment]

    # Track reopened-database read-only verification time inside SQLiteStore.
    # Hook __init__ / close rather than __enter__ / __exit__ because legacy
    # migration.pack_directory manages the verification handle with try/finally.
    real_sq_init = SQLiteStore.__init__
    real_sq_close = SQLiteStore.close

    def timed_sq_init(self: Any, *a: Any, **kw: Any) -> None:
        t0 = time.perf_counter()
        real_sq_init(self, *a, **kw)
        if (
            in_verify_phase["active"]
            and not in_verify_phase["first_ro_timed"]
            and getattr(self, "_read_only", False)
        ):
            self._bench_ro_t0 = t0

    def timed_sq_close(self: Any) -> Any:
        try:
            return real_sq_close(self)
        finally:
            t0 = getattr(self, "_bench_ro_t0", None)
            if t0 is not None:
                stage["reopen_verify_s"] += time.perf_counter() - t0
                in_verify_phase["first_ro_timed"] = True
                self._bench_ro_t0 = None

    SQLiteStore.__init__ = timed_sq_init  # type: ignore[assignment]
    SQLiteStore.close = timed_sq_close  # type: ignore[assignment]

    # Wrap stage 4/5 on direct-to-SQLite path (v2.6.12)
    try:
        from nd2wsi.storage import single_file_writer as sfw

        real_verify = sfw.verify_and_finalize_single_file

        def timed_verify(*a: Any, **kw: Any) -> Any:
            in_verify_phase["active"] = True
            in_verify_phase["first_ro_timed"] = False
            before_ro = stage["reopen_verify_s"]
            t0 = time.perf_counter()
            try:
                return real_verify(*a, **kw)
            finally:
                total_vf = time.perf_counter() - t0
                ro_dt = stage["reopen_verify_s"] - before_ro
                stage["finalize_commit_s"] += max(0.0, total_vf - ro_dt)
                in_verify_phase["active"] = False

        sfw.verify_and_finalize_single_file = timed_verify  # type: ignore[assignment]
    except ImportError:
        pass

    # Wrap stage 4/5 on baseline pack_directory path (v2.6.11 or baseline_pack)
    real_pack = migration_mod.pack_directory

    def timed_pack(*a: Any, **kw: Any) -> Any:
        in_verify_phase["active"] = True
        in_verify_phase["first_ro_timed"] = False
        before_ro = stage["reopen_verify_s"]
        t0 = time.perf_counter()
        try:
            return real_pack(*a, **kw)
        finally:
            total_pk = time.perf_counter() - t0
            ro_dt = stage["reopen_verify_s"] - before_ro
            stage["finalize_commit_s"] += max(0.0, total_pk - ro_dt)
            in_verify_phase["active"] = False

    migration_mod.pack_directory = timed_pack  # type: ignore[assignment]

    real_commit = cache_mod.commit_container

    def timed_commit(*a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return real_commit(*a, **kw)
        finally:
            stage["finalize_commit_s"] += time.perf_counter() - t0

    cache_mod.commit_container = timed_commit  # type: ignore[assignment]

    storage_arg: Any = None
    if arm == "baseline_pack" and not baseline_repo:
        # Fresh instance (non-identity with DEFAULT_STORAGE) triggers directory + pack_directory
        storage_arg = ZarrV2Storage()

    workers_actual = workers_val if workers_val is not None else convert_mod.auto_workers()
    blosc_before = (bool(blosc.use_threads), int(blosc.get_nthreads()))

    with RssSampler() as rss, TempDiskSampler(run_dir) as disk:
        t_start = time.perf_counter()
        convert_mod.convert(
            slide,
            out_path,
            tile=tile_val,
            workers=workers_val,
            overview=overview,
            overwrite=True,
            progress=False,
            storage=storage_arg,
        )
        end_to_end_s = time.perf_counter() - t_start

    blosc_after = (bool(blosc.use_threads), int(blosc.get_nthreads()))
    final_cache_bytes = out_path.stat().st_size
    manifest = cache_mod.read_manifest(out_path) or {}

    # Post-build verification reads & full decoded pixel SHA-256 (outside build RSS/timing)
    post = _measure_viewport_reads(out_path)
    out_path.unlink(missing_ok=True)

    nd2wsi_pkg_dir = Path(convert_mod.__file__).resolve().parent.parent
    return {
        "slide": str(slide),
        "slide_name": slide.name,
        "slide_size_bytes": slide.stat().st_size,
        "mode": mode,
        "arm": arm,
        "baseline_repo": baseline_repo,
        "nd2wsi_repo_commit": _git_head(nd2wsi_pkg_dir),
        "tile_requested": tile_arg,
        "tile_actual": post["tile_actual"],
        "workers_requested": workers_arg,
        "workers_actual": workers_actual,
        "blosc_use_threads": blosc_after[0],
        "blosc_nthreads": blosc_after[1],
        "blosc_use_threads_before": blosc_before[0],
        "used_seq_reader": stage["used_seq_reader"],
        "end_to_end_s": round(end_to_end_s, 4),
        "first_level_s": round(stage["first_level_s"], 4),
        "later_levels_s": round(stage["later_levels_s"], 4),
        "later_levels_per_level_s": stage["later_levels_per_level_s"],
        "omero_windows_s": round(stage["omero_windows_s"], 4),
        "reopen_verify_s": round(stage["reopen_verify_s"], 4),
        "finalize_commit_s": round(stage["finalize_commit_s"], 4),
        "final_cache_bytes": final_cache_bytes,
        "temp_disk_peak_bytes": max(disk.peak_bytes, final_cache_bytes),
        "rss": rss.report(),
        "manifest_format": manifest.get("format"),
        "storage_container": (manifest.get("storage") or {}).get("container"),
        "viewport_p50_ms": post["viewport_p50_ms"],
        "viewport_p95_ms": post["viewport_p95_ms"],
        "levels": post["levels"],
        "level_pixel_sha256": post["level_pixel_sha256"],
        "omero_windows": post["omero_windows"],
    }


# ---------------------------------------------------------------------------
# Parent Orchestrator & Summary Report
# ---------------------------------------------------------------------------


def _run_repeat_subprocess(spec: dict[str, Any]) -> dict[str, Any]:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--_worker-spec", json.dumps(spec)]
    env = os.environ.copy()
    if spec.get("baseline_repo"):
        base = str(Path(spec["baseline_repo"]).resolve())
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{base}{os.pathsep}{existing}" if existing else base
    else:
        repo_root = str(Path(__file__).resolve().parent.parent)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{existing}" if existing else repo_root

    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"benchmark child failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("__BENCH_RESULT__=")]
    if not lines:
        raise RuntimeError(f"benchmark child produced no result marker:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(lines[-1].split("=", 1)[1])


def _aggregate_slide(runs: list[dict[str, Any]]) -> dict[str, Any]:
    first_hashes = runs[0]["level_pixel_sha256"]
    first_windows = runs[0]["omero_windows"]
    hashes_consistent = all(r["level_pixel_sha256"] == first_hashes for r in runs)
    windows_consistent = all(r["omero_windows"] == first_windows for r in runs)
    return {
        "slide": runs[0]["slide"],
        "slide_name": runs[0]["slide_name"],
        "slide_size_bytes": runs[0]["slide_size_bytes"],
        "repeats": len(runs),
        "tile_actual": runs[0]["tile_actual"],
        "workers_actual": runs[0]["workers_actual"],
        "used_seq_reader": runs[0]["used_seq_reader"],
        "blosc_use_threads": runs[0]["blosc_use_threads"],
        "blosc_nthreads": runs[0]["blosc_nthreads"],
        "median_end_to_end_s": round(float(statistics.median(r["end_to_end_s"] for r in runs)), 4),
        "median_first_level_s": round(float(statistics.median(r["first_level_s"] for r in runs)), 4),
        "median_later_levels_s": round(float(statistics.median(r["later_levels_s"] for r in runs)), 4),
        "median_omero_windows_s": round(float(statistics.median(r["omero_windows_s"] for r in runs)), 4),
        "median_reopen_verify_s": round(float(statistics.median(r["reopen_verify_s"] for r in runs)), 4),
        "median_finalize_commit_s": round(
            float(statistics.median(r["finalize_commit_s"] for r in runs)), 4
        ),
        "median_final_cache_bytes": int(
            statistics.median(r["final_cache_bytes"] for r in runs)
        ),
        "median_temp_disk_peak_bytes": int(
            statistics.median(r["temp_disk_peak_bytes"] for r in runs)
        ),
        "median_peak_rss_mib": round(
            float(statistics.median(r["rss"]["peak_rss_mib"] for r in runs)), 1
        ),
        "median_viewport_p50_ms": round(
            float(statistics.median(r["viewport_p50_ms"] for r in runs)), 3
        ),
        "median_viewport_p95_ms": round(
            float(statistics.median(r["viewport_p95_ms"] for r in runs)), 3
        ),
        "hashes_consistent": hashes_consistent,
        "windows_consistent": windows_consistent,
        "level_pixel_sha256": first_hashes,
        "omero_windows": first_windows,
        "manifest_format": runs[0]["manifest_format"],
        "storage_container": runs[0]["storage_container"],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark nd2wsi single-file pyramid cache builds.")
    p.add_argument("--_worker-spec", dest="worker_spec", default=None, help=argparse.SUPPRESS)
    p.add_argument("--slides", nargs="+", help="One or more input slide paths (read-only).")
    p.add_argument(
        "--out-dir",
        help="Parent directory for isolated per-run temporary outputs (never deleted itself).",
    )
    p.add_argument("--mode", choices=("overview", "full"), default="overview")
    p.add_argument("--tile", default="auto", help="Tile edge in pixels or 'auto'.")
    p.add_argument("--workers", default="auto", help="Worker thread count or 'auto'.")
    p.add_argument("--repeats", type=int, default=3, help="Number of repeats per slide.")
    p.add_argument(
        "--arm",
        choices=("baseline_pack", "direct_only", "direct_seqread"),
        default="direct_seqread",
    )
    p.add_argument(
        "--baseline-repo",
        default=None,
        help="Optional v2.6.11 (7993fe1) checkout path for official baseline runs.",
    )
    p.add_argument("--json-out", default=None, help="Optional JSON output report path.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.worker_spec is not None:
        result = _run_worker(json.loads(args.worker_spec))
        print(f"__BENCH_RESULT__={json.dumps(result)}", flush=True)
        return 0

    if not args.slides or not args.out_dir:
        raise SystemExit("error: --slides and --out-dir are required")
    if args.repeats < 1:
        raise SystemExit("error: --repeats must be >= 1")

    slides = [Path(s).resolve() for s in args.slides]
    for s in slides:
        if not s.is_file():
            raise SystemExit(f"error: slide not found: {s}")

    out_parent = Path(args.out_dir).resolve()
    out_parent.mkdir(parents=True, exist_ok=True)
    run_root = out_parent / f"bench-pyramid-{uuid.uuid4().hex[:12]}"
    run_root.mkdir(parents=True, exist_ok=False)

    env_info = {
        "os": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "ram_gib": _total_ram_gib(),
        "output_dir": str(out_parent),
        "output_fs": _filesystem_info(run_root),
        "input_fs": {str(s): _filesystem_info(s) for s in slides},
        "mode": args.mode,
        "arm": args.arm,
        "tile": args.tile,
        "workers": args.workers,
        "repeats": args.repeats,
        "baseline_repo": str(Path(args.baseline_repo).resolve()) if args.baseline_repo else None,
    }

    runs_by_slide: dict[str, list[dict[str, Any]]] = {str(s): [] for s in slides}
    all_runs: list[dict[str, Any]] = []

    try:
        for rep in range(args.repeats):
            order = slides if rep % 2 == 0 else list(reversed(slides))
            for idx, slide in enumerate(order):
                iter_dir = run_root / f"rep{rep}_{idx}_{uuid.uuid4().hex[:6]}"
                iter_dir.mkdir(parents=True, exist_ok=False)
                try:
                    spec = {
                        "slide": str(slide),
                        "run_dir": str(iter_dir),
                        "mode": args.mode,
                        "arm": args.arm,
                        "tile": args.tile,
                        "workers": args.workers,
                        "baseline_repo": env_info["baseline_repo"],
                    }
                    res = _run_repeat_subprocess(spec)
                    res["repeat"] = rep
                    runs_by_slide[str(slide)].append(res)
                    all_runs.append(res)
                    print(
                        f"[rep {rep + 1}/{args.repeats}] {slide.name} ({args.arm}/{args.mode}): "
                        f"e2e={res['end_to_end_s']:.3f}s "
                        f"(L1/L0={res['first_level_s']:.3f}s, later={res['later_levels_s']:.3f}s, "
                        f"win={res['omero_windows_s']:.3f}s, verify={res['reopen_verify_s']:.3f}s, "
                        f"fin={res['finalize_commit_s']:.3f}s) "
                        f"tile={res['tile_actual']} size={res['final_cache_bytes'] / (1024**2):.1f}MiB "
                        f"rss={res['rss']['peak_rss_mib']:.1f}MiB "
                        f"vp_p50={res['viewport_p50_ms']:.2f}ms",
                        flush=True,
                    )
                finally:
                    shutil.rmtree(iter_dir, ignore_errors=True)
    finally:
        shutil.rmtree(run_root, ignore_errors=True)

    summary = {str(s): _aggregate_slide(runs_by_slide[str(s)]) for s in slides}
    report = {"environment": env_info, "summary": summary, "runs": all_runs}

    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\nSummary (medians):")
    print(
        f"  OS={env_info['os']} | out_fs={env_info['output_fs']['fs_type']} "
        f"(blk={env_info['output_fs']['alloc_block_bytes']}) | arm={args.arm} | mode={args.mode}"
    )
    for s in slides:
        row = summary[str(s)]
        in_fs = env_info["input_fs"][str(s)]["fs_type"]
        print(
            f"  {row['slide_name']} [{in_fs} -> {env_info['output_fs']['fs_type']}]: "
            f"e2e={row['median_end_to_end_s']:.3f}s | L1/L0={row['median_first_level_s']:.3f}s | "
            f"later={row['median_later_levels_s']:.3f}s | win={row['median_omero_windows_s']:.3f}s | "
            f"verify={row['median_reopen_verify_s']:.3f}s | fin={row['median_finalize_commit_s']:.3f}s | "
            f"size={row['median_final_cache_bytes'] / (1024**2):.2f}MiB | "
            f"disk_peak={row['median_temp_disk_peak_bytes'] / (1024**2):.2f}MiB | "
            f"rss={row['median_peak_rss_mib']:.1f}MiB | "
            f"vp_p50/p95={row['median_viewport_p50_ms']:.2f}/{row['median_viewport_p95_ms']:.2f}ms | "
            f"seq={row['used_seq_reader']} | tile={row['tile_actual']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
