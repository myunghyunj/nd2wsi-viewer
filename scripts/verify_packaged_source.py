"""Compare packaged Python bytecode and static assets with the frozen checkout.

The defaults describe the released ``nd2wsi-viewer.app``. The options describe a
differently named bundle, such as the historical local betas under
``packaging/legacy``, without a second copy of the comparison.
"""

import argparse
import json
import plistlib
import tomllib
from pathlib import Path
from types import CodeType

ROOT = Path(__file__).resolve().parents[1]
COMPARISON = "Normalized recursive Python code equality and exact static asset bytes"


def normalized(code):
    return code.replace(co_filename="<source>", co_consts=tuple(
        normalized(item) if isinstance(item, CodeType) else item for item in code.co_consts))


def project_version(root=ROOT):
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


def open_archive(app, executable):
    """The PYZ archive embedded in the bundle's main executable (needs PyInstaller)."""
    from PyInstaller.archive.readers import CArchiveReader

    return CArchiveReader(str(app / "Contents/MacOS" / executable)).open_embedded_archive("PYZ.pyz")


def verify(toc, extract, resources, info, *, expected_version, bundle_name="nd2wsi-viewer",
           bundle_id="com.nd2wsi.viewer", require_modules=(), require_resources=(),
           expect_updates_disabled=False, root=ROOT):
    """Check one frozen bundle against the checkout at ``root`` and return the report.

    ``toc`` holds the module names of the PYZ archive, ``extract(module)`` returns a
    module's code object, ``resources`` is the bundle's Contents/Resources directory
    and ``info`` its Info.plist as a dict. Any mismatch raises AssertionError.
    """
    for module in require_modules:
        assert module in toc, f"missing packaged module: {module}"
    for resource in require_resources:
        assert (resources / resource).is_file(), f"missing packaged resource: {resource}"
    checked = []
    for source in sorted((root / "nd2wsi").rglob("*.py")):
        relative = source.relative_to(root)
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(parts)
        actual = normalized(extract(module))
        expected = normalized(compile(source.read_text(), "<source>", "exec"))
        # Compare code structurally: marshal bytes contain irrelevant object
        # reference/interner differences after archive decoding.
        assert actual == expected, f"stale packaged module: {module}"
        checked.append(module)
    assets = []
    for source in sorted((root / "nd2wsi/static").rglob("*")):
        if source.is_file():
            relative = source.relative_to(root)
            assert (resources / relative).read_bytes() == source.read_bytes(), str(relative)
            assets.append(str(relative))
    assert info["ND2WSIPackageVersion"] == expected_version, \
        f"packaged version {info['ND2WSIPackageVersion']!r} is not {expected_version!r}"
    assert info["CFBundleName"] == bundle_name, info["CFBundleName"]
    assert info["CFBundleIdentifier"] == bundle_id, info["CFBundleIdentifier"]
    if expect_updates_disabled:
        assert not info["SUEnableAutomaticChecks"] and "SUFeedURL" not in info, \
            "the bundle must not check for or advertise updates"
    return {"ok": True, "version": info["ND2WSIPackageVersion"], "python_modules": checked,
            "static_assets": assets, "comparison": COMPARISON}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True, help="the frozen .app bundle")
    parser.add_argument("--output", type=Path, required=True, help="new JSON report path")
    parser.add_argument("--executable", default="nd2wsi-viewer",
                        help="main executable name under Contents/MacOS")
    parser.add_argument("--bundle-name", default="nd2wsi-viewer", help="expected CFBundleName")
    parser.add_argument("--bundle-id", default="com.nd2wsi.viewer",
                        help="expected CFBundleIdentifier")
    parser.add_argument("--expected-version",
                        help="expected ND2WSIPackageVersion (default: pyproject.toml)")
    parser.add_argument("--require-module", action="append", metavar="MODULE",
                        help="module that must be in the PYZ archive (repeatable)")
    parser.add_argument("--require-resource", action="append", metavar="PATH",
                        help="file that must exist under Contents/Resources (repeatable)")
    parser.add_argument("--expect-updates-disabled", action="store_true",
                        help="assert SUEnableAutomaticChecks is false and SUFeedURL is absent")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    archive = open_archive(args.app, args.executable)
    info = plistlib.loads((args.app / "Contents/Info.plist").read_bytes())
    expected = project_version() if args.expected_version is None else args.expected_version
    report = verify(archive.toc, archive.extract, args.app / "Contents/Resources", info,
                    expected_version=expected, bundle_name=args.bundle_name,
                    bundle_id=args.bundle_id, require_modules=args.require_module or (),
                    require_resources=args.require_resource or (),
                    expect_updates_disabled=args.expect_updates_disabled)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps({"ok": True, "modules": len(report["python_modules"]),
                      "assets": len(report["static_assets"])}))


if __name__ == "__main__":
    main()
