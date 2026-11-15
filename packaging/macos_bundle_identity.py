"""Derive a macOS bundle's install identity from its source edition."""
from __future__ import annotations

import argparse
import runpy
import tomllib
from pathlib import Path


def bundle_identity(root: Path) -> dict:
    root = Path(root)
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    edition_path = root / "nd2wsi/edition.py"
    edition = runpy.run_path(str(edition_path)) if edition_path.is_file() else None
    name, bundle_id = "nd2wsi-viewer", "com.nd2wsi.viewer"
    if edition is not None:
        if edition.get("NAME") != "SLACS-module" or edition.get("UPDATES_OFF") is not True:
            raise ValueError("Only the SLACS-module edition with public updates disabled may use a lab bundle")
        revision = edition.get("REVISION")
        if type(revision) is not int or revision < 1:
            raise ValueError("SLACS-module revision must be a positive integer")
        name, bundle_id = "nd2wsi-viewer SLACS", "com.nd2wsi.viewer.slacs"
    return {
        "bundle_name": name, "bundle_id": bundle_id, "executable": name,
        "edition": edition["NAME"] if edition else "official",
        "package_version": version,
        "release_version": edition["release_version"](version) if edition else version,
        "updates_off": edition is not None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--field", required=True, choices=("bundle_name", "bundle_id", "executable", "release_version"))
    args = parser.parse_args()
    print(bundle_identity(args.root)[args.field])


if __name__ == "__main__":
    main()
