"""Compare packaged Python bytecode and static assets with the frozen checkout."""

import argparse
import json
import plistlib
from pathlib import Path
from types import CodeType

from PyInstaller.archive.readers import CArchiveReader


def normalized(code):
    return code.replace(co_filename="<source>", co_consts=tuple(
        normalized(item) if isinstance(item, CodeType) else item for item in code.co_consts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    archive = CArchiveReader(str(args.app / "Contents/MacOS/nd2wsi-viewer")).open_embedded_archive("PYZ.pyz")
    checked = []
    for source in sorted((root / "nd2wsi").rglob("*.py")):
        relative = source.relative_to(root)
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(parts)
        actual = normalized(archive.extract(module))
        expected = normalized(compile(source.read_text(), "<source>", "exec"))
        # Compare code structurally: marshal bytes contain irrelevant object
        # reference/interner differences after archive decoding.
        assert actual == expected, f"stale packaged module: {module}"
        checked.append(module)
    assets = []
    for source in sorted((root / "nd2wsi/static").rglob("*")):
        if source.is_file():
            relative = source.relative_to(root)
            assert (args.app / "Contents/Resources" / relative).read_bytes() == source.read_bytes(), str(relative)
            assets.append(str(relative))
    info = plistlib.loads((args.app / "Contents/Info.plist").read_bytes())
    assert info["ND2WSIPackageVersion"] == "2.1.0rc2"
    assert info["CFBundleName"] == "nd2wsi-viewer"
    assert info["CFBundleIdentifier"] == "com.nd2wsi.viewer"
    report = {"ok": True, "version": info["ND2WSIPackageVersion"], "python_modules": checked,
              "static_assets": assets, "comparison": "Normalized recursive Python code equality and exact static asset bytes"}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps({"ok": True, "modules": len(checked), "assets": len(assets)}))


if __name__ == "__main__":
    main()
