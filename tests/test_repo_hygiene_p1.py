"""Repository hygiene, documentation link/anchor resolution, licensing, citation, and CLI checks."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from nd2wsi.cli import main as cli_main

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_AUTOMATION_TOKENS = (
    "--agent-window",
    'new_window("agent")',
    "new_window('agent')",
    "window_context",
    "New Agent Window",
    "exports_root",
)


def _github_slug(heading_text: str) -> str:
    """Approximate GitHub Flavored Markdown heading anchor slug."""
    # Strip inline HTML tags and markdown link wrappers/code backticks/emphasis.
    clean = re.sub(r"<[^>]+>", "", heading_text)
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
    clean = clean.replace("`", "").replace("*", "").replace("_", "")
    clean = clean.strip().lower()
    # Keep word chars (letters/digits, unicode-aware without underscore), spaces, and hyphens.
    slug_chars: list[str] = []
    for ch in clean:
        if ch.isalnum():
            slug_chars.append(ch)
        elif ch in (" ", "-"):
            slug_chars.append("-")
        # Other punctuation (&, /, ., etc.) is dropped by GitHub's slugger
    return "".join(slug_chars).strip("-")


def _markdown_anchors(md_path: Path) -> set[str]:
    text = md_path.read_text(encoding="utf-8")
    anchors: set[str] = set()
    # ATX headings outside fenced code blocks
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
    # Explicit HTML id / name attributes (<a id="...">, <h1 id="...">, etc.)
    for m in re.finditer(r'\b(?:id|name)=["\']([^"\']+)["\']', text):
        anchors.add(m.group(1))
    return anchors


def _extract_links(md_path: Path) -> list[str]:
    text = md_path.read_text(encoding="utf-8")
    # Strip fenced code blocks so example paths inside code blocks are ignored
    stripped = re.sub(r"```.*?```", "", text, flags=re.S)
    targets: list[str] = []
    # Markdown inline links & images: [label](target) or ![alt](target)
    for m in re.finditer(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", stripped):
        targets.append(m.group(1))
    # HTML href / src attributes
    for m in re.finditer(r'\b(?:href|src)=["\']([^"\']+)["\']', stripped):
        targets.append(m.group(1))
    return targets


@pytest.mark.parametrize(
    "doc_rel",
    [
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "docs/user-guide.md",
        "docs/release-notes-2.6.4.md",
        "docs/release-notes-2.6.5.md",
        "docs/release-notes-2.6.6.md",
        "docs/release-notes-2.6.7.md",
        "docs/release-notes-2.6.8.md",
        "docs/release-notes-2.6.9.md",
        "docs/release-notes-2.6.10.md",
        "docs/release-notes-2.6.11.md",
        "docs/release-notes-2.6.12.md",
    ],
)
def test_relative_links_and_anchors_resolve(doc_rel: str) -> None:
    doc_path = ROOT / doc_rel
    assert doc_path.is_file(), f"missing documentation file: {doc_rel}"
    targets = _extract_links(doc_path)
    assert targets or doc_rel in (
        "docs/release-notes-2.6.4.md",
        "docs/release-notes-2.6.5.md",
        "docs/release-notes-2.6.6.md",
        "docs/release-notes-2.6.7.md",
        "docs/release-notes-2.6.8.md",
        "docs/release-notes-2.6.9.md",
        "docs/release-notes-2.6.10.md",
        "docs/release-notes-2.6.11.md",
        "docs/release-notes-2.6.12.md",
    )

    for raw in targets:
        if raw.startswith(("http://", "https://", "mailto:", "data:")):
            continue
        path_part, sep, frag = raw.partition("#")
        target_file = (doc_path.parent / path_part).resolve() if path_part else doc_path.resolve()
        assert target_file.exists(), f"{doc_rel}: broken relative link {raw!r} -> {target_file}"
        if sep and frag:
            assert target_file.suffix.lower() == ".md", (
                f"{doc_rel}: anchor #{frag} points to non-markdown file {target_file}"
            )
            valid_anchors = _markdown_anchors(target_file)
            assert frag in valid_anchors, (
                f"{doc_rel}: unresolved anchor #{frag!r} in {target_file.relative_to(ROOT)} "
                f"(available: {sorted(valid_anchors)})"
            )


@pytest.mark.parametrize(
    "doc_rel",
    [
        "README.md",
        "docs/user-guide.md",
        "docs/release-notes-2.6.4.md",
        "docs/release-notes-2.6.5.md",
        "docs/release-notes-2.6.6.md",
        "docs/release-notes-2.6.7.md",
        "docs/release-notes-2.6.8.md",
        "docs/release-notes-2.6.9.md",
        "docs/release-notes-2.6.10.md",
        "docs/release-notes-2.6.11.md",
        "docs/release-notes-2.6.12.md",
        "packaging/WINDOWS-README.txt",
        "THIRD_PARTY_NOTICES.md",
    ],
)
def test_public_docs_never_mention_internal_agent_automation(doc_rel: str) -> None:
    content = (ROOT / doc_rel).read_text(encoding="utf-8")
    for forbidden in FORBIDDEN_AUTOMATION_TOKENS:
        assert forbidden not in content, f"{doc_rel} leaked internal token {forbidden!r}"


def test_readme_positioning_and_sections_are_synced() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for fmt in ("ND2", "CZI", "LIF", "OME-TIFF / TIFF", "SVS", "SDPC", "ND2SVS", "JPG", "PNG"):
        assert fmt in readme
    assert ">☆<" not in readme
    assert ">Astromap<" in readme
    assert "## Latest update" not in readme
    assert "## Recent updates" in readme
    for ver in ("2.6.12", "2.6.11", "2.6.10", "2.6.9", "2.6.8", "2.6.7", "2.6.6", "2.6.5", "2.6.4", "2.6.3", "2.6.2", "2.6.1", "2.6.0"):
        assert f"docs/release-notes-{ver}.md" in readme
    assert (
        'src="docs/overview-sdpc.png" '
        'alt="Brightfield SVS slide with Slide Info in the light appearance"'
    ) in readme
    assert "### Z-stacks, time series and plates" in readme
    assert "### Annotations & Astromap" in readme
    assert "The standard viewer" not in readme
    assert "THIRD_PARTY_NOTICES.md" in readme


def test_license_is_pure_spdx_mit_and_third_party_notices_separate() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8").strip()
    lines = license_text.splitlines()
    assert lines[0] == "MIT License"
    assert lines[-1] == "SOFTWARE."
    assert "Permission is hereby granted, free of charge" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in license_text
    assert "---" not in license_text
    assert "OpenSeadragon" not in license_text
    assert "Bundled third-party" not in license_text

    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "OpenSeadragon" in notices
    assert "BSD-3-Clause" in notices
    assert "Icons8" in notices
    assert "LGPL-2.1-or-later" in notices
    assert "FFmpeg" in notices
    assert "PyAV" in notices


def test_citation_cff_parses_and_matches_project_metadata() -> None:
    cff_path = ROOT / "CITATION.cff"
    assert cff_path.is_file()
    cff = yaml.safe_load(cff_path.read_text(encoding="utf-8"))
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert str(cff["cff-version"]) == "1.2.0"
    assert cff["title"] == "nd2wsi-viewer"
    assert cff["license"] == "MIT"
    assert cff["version"] == pyproject["version"]
    assert cff["repository-code"] == "https://github.com/myunghyunj/nd2wsi-viewer"
    assert cff["authors"] == [{"family-names": "Jeong", "given-names": "Myunghyun"}]
    keywords = set(cff["keywords"])
    for expected in ("microscopy", "whole-slide-imaging", "nd2", "czi", "lif", "ome-tiff", "svs"):
        assert expected in keywords


def test_pyproject_release_notes_and_windows_readme_metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    desc = project["description"]
    for token in ("Nikon ND2", "Zeiss CZI", "Leica LIF", "OME-TIFF", "Aperio SVS", "SDPC"):
        assert token in desc
    kw = set(project["keywords"])
    for expected in ("czi", "zeiss", "lif", "leica", "ome-tiff", "confocal", "z-stack"):
        assert expected in kw

    rn_264 = (ROOT / "docs" / "release-notes-2.6.4.md").read_text(encoding="utf-8")
    rn_263 = (ROOT / "docs" / "release-notes-2.6.3.md").read_text(encoding="utf-8")
    downloads_263 = "### Downloads\n" + rn_263.split("### Downloads\n", 1)[1]
    assert rn_264.endswith(downloads_263)

    from nd2wsi import update_card

    items_264 = update_card.release_note_items(rn_264)
    assert len(items_264) == 4
    assert items_264[0]["title"] == "Review individual rows"

    win_readme = (ROOT / "packaging" / "WINDOWS-README.txt").read_text(encoding="utf-8")
    for fmt in ("ND2", "CZI", "LIF", "OME-TIFF", "SVS", "SDPC", "ND2SVS", "JPG", "PNG"):
        assert fmt in win_readme


def test_cli_help_and_subcommands_work(capsys: pytest.CaptureFixture[str]) -> None:
    for sub_args in (
        ["--help"],
        ["info", "--help"],
        ["convert", "--help"],
        ["crop", "--help"],
        ["tidy", "--help"],
        ["serve", "--help"],
        ["view", "--help"],
    ):
        with pytest.raises(SystemExit) as exc:
            cli_main(sub_args)
        assert exc.value.code == 0

    out = capsys.readouterr().out
    for expected in ("Zeiss CZI", "Leica LIF", "OME-TIFF", ".nd2svs", "--remove-corrupt-caches"):
        assert expected in out

    proc = subprocess.run(
        [sys.executable, "-m", "nd2wsi.cli", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "Zeiss CZI" in proc.stdout
    assert "Leica LIF" in proc.stdout
