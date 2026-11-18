"""Tests for scripts/benchmark_milestone.py CLI modes, sandbox non-mutation, RSS gate, and protocol doc hygiene."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import tifffile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "benchmark_milestone.py"
PROTOCOL_DOC = ROOT / "docs" / "milestone-1-validation-protocol.md"

FORBIDDEN_AUTOMATION_TOKENS = (
    "--agent-window",
    'new_window("agent")',
    "new_window('agent')",
    "window_context",
    "New Agent Window",
    "exports_root",
    "AGENTS.md",
    "run_native_check.py",
)


def _load_bench_module():
    spec = importlib.util.spec_from_file_location("benchmark_milestone", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _isolated_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bench_tmp = tmp_path / "bench_sandbox"
    app_sup = tmp_path / "app_support"
    pyc_dir = tmp_path / "pycache"
    sys_tmp = tmp_path / "sys_tmp"
    for d in (bench_tmp, app_sup, pyc_dir, sys_tmp):
        d.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["ND2WSI_BENCH_TMP"] = str(bench_tmp)
    env["TMPDIR"] = str(sys_tmp)
    env["TEMP"] = str(sys_tmp)
    env["TMP"] = str(sys_tmp)
    env["ND2WSI_APP_SUPPORT_DIR"] = str(app_sup)
    env["ND2WSI_UI_SETTINGS_ROOT"] = str(app_sup)
    env["ND2WSI_LUT_SETTINGS_ROOT"] = str(app_sup / "lut-settings")
    env["ND2WSI_WINDOW_SESSION_ROOT"] = str(app_sup / "window-sessions")
    env["PYTHONPYCACHEPREFIX"] = str(pyc_dir)
    return env, bench_tmp


def _github_slug(heading_text: str) -> str:
    clean = re.sub(r"<[^>]+>", "", heading_text)
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
    clean = clean.replace("`", "").replace("*", "").replace("_", "")
    clean = clean.strip().lower()
    slug_chars: list[str] = []
    for ch in clean:
        if ch.isalnum():
            slug_chars.append(ch)
        elif ch in (" ", "-"):
            slug_chars.append("-")
    return "".join(slug_chars).strip("-")


def _markdown_anchors(md_path: Path) -> set[str]:
    text = md_path.read_text(encoding="utf-8")
    anchors: set[str] = set()
    in_fence = False
    slug_counts: dict[str, int] = {}
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if m:
            base = _github_slug(m.group(1))
            if base:
                count = slug_counts.get(base, 0)
                slug_counts[base] = count + 1
                anchors.add(base if count == 0 else f"{base}-{count}")
    for m in re.finditer(r'\b(?:id|name)=["\']([^"\']+)["\']', text):
        anchors.add(m.group(1))
    return anchors


def _extract_links(md_path: Path) -> list[str]:
    text = md_path.read_text(encoding="utf-8")
    stripped = re.sub(r"```.*?```", "", text, flags=re.S)
    targets: list[str] = []
    for m in re.finditer(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", stripped):
        targets.append(m.group(1))
    for m in re.finditer(r'\b(?:href|src)=["\']([^"\']+)["\']', stripped):
        targets.append(m.group(1))
    return targets


def test_cli_help_documents_public_modes_and_hides_child_flags(tmp_path: Path) -> None:
    env, _ = _isolated_env(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    out = proc.stdout
    for expected in ("--suite", "smoke", "standard", "scale", "--files", "--max-4tab-rss-mib", "--json-out"):
        assert expected in out
    assert "--child-scenario" not in out
    assert "--target-root" not in out


def test_stage_isolated_slide_refuses_in_place_and_avoids_symlinks(tmp_path: Path) -> None:
    mod = _load_bench_module()
    src_dir = tmp_path / "real_lab_drive"
    src_dir.mkdir()
    src_file = src_dir / "sample.ome.tif"
    tifffile.imwrite(str(src_file), np.zeros((2, 64, 64), dtype=np.uint16), ome=True)

    with pytest.raises(ValueError, match="Refusing to stage slide inside its own source directory"):
        mod.stage_isolated_slide(src_file, src_dir)

    sandbox = tmp_path / "sandbox"
    staged = mod.stage_isolated_slide(src_file, sandbox)
    assert staged.is_file()
    assert not staged.is_symlink()
    assert staged.resolve().parent == sandbox.resolve()
    assert staged.resolve().parent != src_dir.resolve()


def test_smoke_suite_passes_gate_and_cleans_temp(tmp_path: Path) -> None:
    env, bench_tmp = _isolated_env(tmp_path)
    out_json = tmp_path / "smoke_out.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--suite",
            "smoke",
            "--json-out",
            str(out_json),
        ],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "[PASS]" in proc.stdout
    assert list(bench_tmp.iterdir()) == []

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["temp_cleaned"] is True
    res = payload["results"]
    assert res["mode"] == "smoke"
    assert res["gate"]["passed"] is True
    assert res["gate"]["max_4tab_rss_mib_limit"] == pytest.approx(1228.8)
    assert 0.0 < res["gate"]["observed_4tab_peak_rss_mib"] <= 1228.8
    assert res["roundtrip"]["smoke_lif_multiseries"]["reopen_roi_supported"] is True
    assert res["zstack"]["smoke_czi_zstack"]["lingering_prefetch_threads"] == 0


def test_max_4tab_rss_gate_failure_returns_nonzero_and_cleans_temp(tmp_path: Path) -> None:
    env, bench_tmp = _isolated_env(tmp_path)
    out_json = tmp_path / "gate_fail.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--suite",
            "smoke",
            "--max-4tab-rss-mib",
            "1.0",
            "--json-out",
            str(out_json),
        ],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "[FAIL]" in proc.stdout
    assert list(bench_tmp.iterdir()) == []

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["temp_cleaned"] is True
    gate = payload["results"]["gate"]
    assert gate["passed"] is False
    assert gate["max_4tab_rss_mib_limit"] == pytest.approx(1.0)
    assert gate["observed_4tab_peak_rss_mib"] > 1.0


def test_files_mode_preserves_source_directory_untouched(tmp_path: Path) -> None:
    mod = _load_bench_module()
    env, bench_tmp = _isolated_env(tmp_path)

    # Create a simulated user acquisition directory with a CZI slide (which builds an
    # nd2wsi/caches/*.nd2svs store on open) and an OME-TIFF slide.
    user_dir = tmp_path / "external_bench_drive"
    user_dir.mkdir()
    rng = np.random.default_rng(99)
    plane0 = rng.integers(100, 3000, size=(256, 256), dtype=np.uint16)
    plane1 = rng.integers(100, 3000, size=(256, 256), dtype=np.uint16)

    czi_slide = mod.build_synthetic_czi(
        user_dir / "user_acquisition.czi",
        {(0, 0, 0): plane0, (0, 1, 0): plane1},
        compression=5,
        pixel_size_um=(0.25, 0.25),
    )
    ome_slide = user_dir / "user_acquisition.ome.tif"
    tifffile.imwrite(
        str(ome_slide),
        np.stack([plane0, plane1], axis=0),
        photometric="minisblack",
        tile=(128, 128),
        compression="zlib",
        ome=True,
        metadata={
            "axes": "CYX",
            "PhysicalSizeX": 0.325,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": 0.325,
            "PhysicalSizeYUnit": "µm",
            "Channel": {"Name": ["DAPI", "FITC"]},
        },
    )

    before_entries = sorted(p.name for p in user_dir.iterdir())
    before_stats = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in user_dir.iterdir()
    }

    out_json = tmp_path / "reports" / "files_bench.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--files",
            str(czi_slide),
            str(ome_slide),
            "--json-out",
            str(out_json),
        ],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

    after_entries = sorted(p.name for p in user_dir.iterdir())
    after_stats = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in user_dir.iterdir()
    }
    assert after_entries == ["user_acquisition.czi", "user_acquisition.ome.tif"]
    assert after_entries == before_entries
    assert after_stats == before_stats
    assert not (user_dir / "nd2wsi").exists()
    assert list(bench_tmp.iterdir()) == []

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    res = payload["results"]
    assert res["mode"] == "files"
    assert res["source_dir_unchanged"] is True
    assert res["gate"]["passed"] is True
    for rt_entry in res["roundtrip"].values():
        assert rt_entry["supported"] is True
        assert rt_entry["reopen_roi_supported"] is True


def test_validation_protocol_doc_links_anchors_and_hygiene() -> None:
    assert PROTOCOL_DOC.is_file(), f"Missing {PROTOCOL_DOC.relative_to(ROOT)}"
    text = PROTOCOL_DOC.read_text(encoding="utf-8")

    # 1. Zero leakage of internal AGENTS.md automation directions
    for token in FORBIDDEN_AUTOMATION_TOKENS:
        assert token not in text, (
            f"Forbidden internal automation token {token!r} found in {PROTOCOL_DOC.name}"
        )

    # 2. All relative links and #anchor fragments resolve cleanly
    targets = _extract_links(PROTOCOL_DOC)
    assert len(targets) >= 3, "Expected relative cross-references in validation protocol doc"
    for raw in targets:
        if raw.startswith(("http://", "https://", "mailto:", "data:")):
            continue
        path_part, sep, frag = raw.partition("#")
        target_file = (
            (PROTOCOL_DOC.parent / path_part).resolve() if path_part else PROTOCOL_DOC.resolve()
        )
        assert target_file.exists(), (
            f"{PROTOCOL_DOC.name}: broken relative link {raw!r} -> {target_file}"
        )
        if sep and frag:
            assert target_file.suffix.lower() == ".md", (
                f"{PROTOCOL_DOC.name}: anchor #{frag} points to non-markdown file {target_file}"
            )
            valid_anchors = _markdown_anchors(target_file)
            assert frag in valid_anchors, (
                f"{PROTOCOL_DOC.name}: unresolved anchor #{frag!r} in "
                f"{target_file.relative_to(ROOT)} (available: {sorted(valid_anchors)})"
            )

    # 3. Required Milestone 1 scale gate, 5-lab protocol, and validation-gated workstream content
    required_phrases = (
        "--suite scale",
        "--files",
        "1,228.8 MiB",
        "1.2 GiB",
        "0.10071",
        "0.03905",
        "3 / 5",
        "forum.image.sc",
        "March 2027",
        "Pearson",
        "Manders",
        "NIS-Elements",
        "ZEN",
        "LAS X",
    )
    for phrase in required_phrases:
        assert phrase in text, f"Missing required protocol specification {phrase!r} in {PROTOCOL_DOC.name}"
