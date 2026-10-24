"""Exercise the packaged-source verifier against a fake archive, without PyInstaller."""

import importlib.util
import json
import plistlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
STATIC_ASSETS = sorted(str(path.relative_to(ROOT))
                       for path in (ROOT / "nd2wsi/static").rglob("*") if path.is_file())


@pytest.fixture(scope="module")
def verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_packaged_source", ROOT / "scripts/verify_packaged_source.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def compiled():
    """Code objects of every nd2wsi module, keyed the way the PYZ archive names them."""
    code = {}
    for source in sorted((ROOT / "nd2wsi").rglob("*.py")):
        parts = list(source.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        # A frozen module carries the build machine's filename, never "<source>".
        code[".".join(parts)] = compile(source.read_text(encoding="utf-8"), str(source), "exec")
    return code


@pytest.fixture
def app(tmp_path):
    """A bundle skeleton whose Contents/Resources holds the real static assets."""
    bundle = tmp_path / "nd2wsi-viewer.app"
    shutil.copytree(ROOT / "nd2wsi/static", bundle / "Contents/Resources/nd2wsi/static")
    return bundle


class FakeArchive:
    def __init__(self, code, extra=()):
        self.code = dict(code)
        self.toc = set(self.code) | set(extra)

    def extract(self, name):
        return self.code[name]


def release_info(**overrides):
    info = {"CFBundleName": "nd2wsi-viewer", "CFBundleIdentifier": "com.nd2wsi.viewer",
            "ND2WSIPackageVersion": VERSION, "SUEnableAutomaticChecks": True,
            "SUFeedURL": "https://example.invalid/appcast.xml"}
    info.update(overrides)
    return info


def check(verifier, archive, app, info, **options):
    options.setdefault("expected_version", VERSION)
    return verifier.verify(archive.toc, archive.extract, app / "Contents/Resources", info, **options)


def test_release_bundle_passes_with_defaults(verifier, compiled, app):
    report = check(verifier, FakeArchive(compiled), app, release_info())
    assert list(report) == ["ok", "version", "python_modules", "static_assets", "comparison"]
    assert report["ok"] is True and report["version"] == VERSION
    assert sorted(report["python_modules"]) == sorted(compiled)
    assert {"nd2wsi", "nd2wsi.app"} <= set(report["python_modules"])
    assert sorted(report["static_assets"]) == STATIC_ASSETS
    assert report["comparison"] == (
        "Normalized recursive Python code equality and exact static asset bytes")


def test_stale_module_is_detected(verifier, compiled, app):
    stale = dict(compiled)
    source = (ROOT / "nd2wsi/__init__.py").read_text(encoding="utf-8") + "\nSTALE_BUILD = True\n"
    stale["nd2wsi"] = compile(source, "<source>", "exec")
    with pytest.raises(AssertionError, match=r"stale packaged module: nd2wsi$"):
        check(verifier, FakeArchive(stale), app, release_info())


def test_changed_static_asset_is_detected(verifier, compiled, app):
    asset = app / "Contents/Resources" / STATIC_ASSETS[0]
    asset.write_bytes(asset.read_bytes() + b"\n")
    with pytest.raises(AssertionError, match=STATIC_ASSETS[0]):
        check(verifier, FakeArchive(compiled), app, release_info())


def test_required_module_must_be_in_the_archive(verifier, compiled, app):
    required = ["reportlab.pdfgen.canvas", "pypdf"]
    with pytest.raises(AssertionError, match="missing packaged module: reportlab.pdfgen.canvas"):
        check(verifier, FakeArchive(compiled), app, release_info(), require_modules=required)
    archive = FakeArchive(compiled, extra=required)
    assert check(verifier, archive, app, release_info(), require_modules=required)["ok"]


def test_required_resource_must_be_a_file(verifier, compiled, app):
    font = "reportlab/fonts/Vera.ttf"
    with pytest.raises(AssertionError, match="missing packaged resource: reportlab/fonts/Vera.ttf"):
        check(verifier, FakeArchive(compiled), app, release_info(), require_resources=[font])
    (app / "Contents/Resources/reportlab/fonts").mkdir(parents=True)
    (app / "Contents/Resources" / font).write_bytes(b"font")
    assert check(verifier, FakeArchive(compiled), app, release_info(), require_resources=[font])["ok"]


def test_updates_disabled_rejects_a_feed_url_or_automatic_checks(verifier, compiled, app):
    archive = FakeArchive(compiled)
    beta = release_info(SUEnableAutomaticChecks=False)
    # The release bundle legitimately carries a feed, so the check is opt-in.
    assert check(verifier, archive, app, beta)["ok"]
    with pytest.raises(AssertionError, match="must not check for or advertise updates"):
        check(verifier, archive, app, beta, expect_updates_disabled=True)
    del beta["SUFeedURL"]
    assert check(verifier, archive, app, beta, expect_updates_disabled=True)["ok"]
    beta["SUEnableAutomaticChecks"] = True
    with pytest.raises(AssertionError, match="must not check for or advertise updates"):
        check(verifier, archive, app, beta, expect_updates_disabled=True)


def test_expected_version_overrides_the_project_version(verifier, compiled, app):
    archive = FakeArchive(compiled)
    info = release_info(ND2WSIPackageVersion="2.4.0b1")
    with pytest.raises(AssertionError, match=r"'2\.4\.0b1' is not"):
        check(verifier, archive, app, info)
    report = check(verifier, archive, app, info, expected_version="2.4.0b1")
    assert report["ok"] is True and report["version"] == "2.4.0b1"


def test_bundle_name_and_identifier_are_checked(verifier, compiled, app):
    archive = FakeArchive(compiled)
    beta = release_info(CFBundleName="nd2wsi-viewer Astromap Beta",
                        CFBundleIdentifier="com.nd2wsi.viewer.astromap-beta")
    with pytest.raises(AssertionError, match="Astromap Beta"):
        check(verifier, archive, app, beta)
    with pytest.raises(AssertionError, match="astromap-beta"):
        check(verifier, archive, app, beta, bundle_name="nd2wsi-viewer Astromap Beta")
    assert check(verifier, archive, app, beta, bundle_name="nd2wsi-viewer Astromap Beta",
                 bundle_id="com.nd2wsi.viewer.astromap-beta")["ok"]


def test_cli_defaults_verify_the_release_bundle_and_write_the_report(
        verifier, compiled, app, monkeypatch, capsys):
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(release_info()))
    opened = []

    def open_archive(bundle, executable):
        opened.append((bundle, executable))
        return FakeArchive(compiled)

    monkeypatch.setattr(verifier, "open_archive", open_archive)
    output = app.parent / "packaged-source.json"
    verifier.main(["--app", str(app), "--output", str(output)])
    assert opened == [(app, "nd2wsi-viewer")]
    report = json.loads(output.read_text(encoding="utf-8"))
    assert list(report) == ["ok", "version", "python_modules", "static_assets", "comparison"]
    assert report["ok"] is True and report["version"] == VERSION
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "modules": len(compiled), "assets": len(STATIC_ASSETS)}
    # An existing report is never overwritten.
    with pytest.raises(FileExistsError):
        verifier.main(["--app", str(app), "--output", str(output)])


def test_cli_beta_parameters_match_the_legacy_astromap_bundle(verifier, compiled, app, monkeypatch):
    name, bundle_id = "nd2wsi-viewer Astromap Beta", "com.nd2wsi.viewer.astromap-beta"
    info = {"CFBundleName": name, "CFBundleIdentifier": bundle_id,
            "ND2WSIPackageVersion": "2.4.0b1", "SUEnableAutomaticChecks": False}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    (app / "Contents/Resources/reportlab/fonts").mkdir(parents=True)
    (app / "Contents/Resources/reportlab/fonts/Vera.ttf").write_bytes(b"font")
    required = ["reportlab.pdfgen.canvas", "reportlab.pdfbase.ttfonts", "pypdf", "charset_normalizer"]
    opened = []

    def open_archive(bundle, executable):
        opened.append(executable)
        return FakeArchive(compiled, extra=required)

    monkeypatch.setattr(verifier, "open_archive", open_archive)
    with pytest.raises(AssertionError):
        verifier.main(["--app", str(app), "--output", str(app.parent / "defaults.json")])
    output = app.parent / "packaged-source.json"
    argv = ["--app", str(app), "--output", str(output), "--executable", name,
            "--bundle-name", name, "--bundle-id", bundle_id, "--expected-version", "2.4.0b1",
            "--require-resource", "reportlab/fonts/Vera.ttf", "--expect-updates-disabled"]
    for module in required:
        argv += ["--require-module", module]
    verifier.main(argv)
    assert opened[-1] == name
    assert json.loads(output.read_text(encoding="utf-8"))["version"] == "2.4.0b1"


def test_legacy_astromap_recipe_calls_the_shared_verifier():
    text = (ROOT / "packaging/legacy/build_astromap_beta.sh").read_text(encoding="utf-8")
    assert "CArchiveReader" not in text
    assert '"$REPO/scripts/verify_packaged_source.py"' in text
    assert '--output "$STAGE/packaged-source.json"' in text
    for option in ('--executable "$NAME"', '--bundle-name "$NAME"', '--bundle-id "$BUNDLE_ID"',
                   '--expected-version "$VERSION"', "--require-module reportlab.pdfgen.canvas",
                   "--require-module reportlab.pdfbase.ttfonts", "--require-module pypdf",
                   "--require-module charset_normalizer",
                   "--require-resource reportlab/fonts/Vera.ttf", "--expect-updates-disabled"):
        assert option in text


@pytest.mark.parametrize("builder, name, version, short", [
    ("build_astromap_beta.sh", "nd2wsi-viewer Astromap Beta", "2.4.0b1", "2.4.0"),
    ("build_metal_beta.sh", "nd2wsi-viewer Metal Beta", "2.1.0b2", "2.1.0"),
])
def test_legacy_plist_heredoc_takes_the_version_from_argv(tmp_path, builder, name, version, short):
    """Execute only the pure plist writer of each frozen recipe, never the build."""
    text = (ROOT / "packaging/legacy" / builder).read_text(encoding="utf-8")
    assert f'VERSION="{version}"' in text, "one VERSION variable at the top"
    assert f"'{version}'" not in text and f'"{version}"' not in text.replace(f'VERSION="{version}"', "")
    source = text.split('"$APP/Contents/Info.plist"', 1)[1].split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    target = tmp_path / "Info.plist"
    target.write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.nd2wsi.viewer",
                                       "SUFeedURL": "https://example.invalid/appcast.xml"}))
    argv = [str(target), name, version]
    if builder == "build_astromap_beta.sh":
        argv.insert(2, "com.nd2wsi.viewer.astromap-beta")
    subprocess.run([sys.executable, "-", *argv], input=source, text=True, check=True)
    info = plistlib.loads(target.read_bytes())
    assert info["ND2WSIPackageVersion"] == version and info["CFBundleVersion"] == version
    assert info["CFBundleShortVersionString"] == short
    assert info["CFBundleDisplayName"] == name
    assert info["SUEnableAutomaticChecks"] is False and "SUFeedURL" not in info
    if builder == "build_astromap_beta.sh":
        assert info["CFBundleName"] == name
        assert info["CFBundleIdentifier"] == "com.nd2wsi.viewer.astromap-beta"
        assert info["ND2WSIDevelopmentOnly"] is True
