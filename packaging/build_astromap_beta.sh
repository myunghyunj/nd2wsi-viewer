#!/bin/bash
# Build a separate local beta. No install, release signing, upload, or appcast.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
OUT="${1:?usage: build_astromap_beta.sh /absolute/new-output-directory}"
PY="${ND2WSI_BUILD_PYTHON:-$REPO/.venv/bin/python}"
TOOLS="${ND2WSI_BUILD_TOOLS:-$REPO/qa/build-tools}"
RUNTIME="${ND2WSI_BUILD_RUNTIME:-$REPO/qa/runtime}"
CODEC_APP="${ND2WSI_CODEC_APP:-}"
NAME="nd2wsi-viewer Astromap Beta"
VERSION="2.4.0b1"
[[ "$(uname -sm)" == "Darwin arm64" ]] || { echo "macOS arm64 is required" >&2; exit 2; }
[[ "$OUT" == /* && ! -e "$OUT" ]] || { echo "Choose a new absolute output directory" >&2; exit 2; }
[[ -x "$PY" ]] || { echo "Set ND2WSI_BUILD_PYTHON to the prepared Python" >&2; exit 2; }

# Clean pywebview resources take precedence over any synced older dependency
# folder. All project imports must resolve to this checkout, never another
# editable install. Build dependencies are isolated, not installed here.
export PYTHONPATH="$RUNTIME:$TOOLS:$REPO${PYTHONPATH:+:$PYTHONPATH}"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$REPO/build/astromap-config}"
# Optionally reuse the exact source-built codec from a verified installed
# viewer. The baseline is read-only; copied extensions must match this Python
# ABI and pass the application's own runtime-license check below.
if [[ -n "$CODEC_APP" ]]; then
  "$PY" - "$CODEC_APP" "$RUNTIME" <<'PY'
from pathlib import Path
import shutil
import sys

app, runtime = map(Path, sys.argv[1:])
resources, frameworks = app / 'Contents/Resources', app / 'Contents/Frameworks'
if not (resources / 'sdpc-codec-manifest.json').is_file():
    raise SystemExit('The codec baseline has no source-built codec manifest')
runtime.mkdir(parents=True, exist_ok=True)
for name in ('av', 'av-16.0.1.dist-info'):
    if (runtime / name).exists():
        shutil.rmtree(runtime / name)
    shutil.copytree(resources / name, runtime / name, symlinks=False,
                    ignore=shutil.ignore_patterns('* 2.*', '__pycache__'))
for name in ('libavcodec', 'libavdevice', 'libavfilter', 'libavformat',
             'libavutil', 'libswresample', 'libswscale'):
    files = list(frameworks.glob(name + '.*.dylib'))
    if len(files) != 1:
        raise SystemExit(f'Ambiguous baseline codec library: {name}')
    shutil.copy2(files[0], runtime / files[0].name)
PY
fi
"$PY" - "$REPO" "$RUNTIME" <<'PY'
import importlib.util
import pathlib
import sys
import nd2wsi
import webview
import av
from nd2wsi.smoke import _verify_sdpc_runtime_license

assert pathlib.Path(nd2wsi.__file__).resolve().parent == pathlib.Path(sys.argv[1]) / 'nd2wsi'
for name in ('PyInstaller', 'numpy', 'scipy', 'nd2', 'zarr', 'numcodecs', 'dask',
             'tifffile', 'imagecodecs', 'limnd2', 'av', '_tkinter',
             'reportlab', 'pypdf', 'charset_normalizer'):
    if importlib.util.find_spec(name) is None:
        raise SystemExit(f'Missing build dependency: {name}')
scripts = pathlib.Path(webview.__file__).parent / 'js'
if list(scripts.rglob('* 2.js')):
    raise SystemExit('Duplicate pywebview resources: provide a clean ND2WSI_BUILD_RUNTIME')
_verify_sdpc_runtime_license(av)
print('Building current checkout:', nd2wsi.__file__)
print('Using pywebview:', webview.__file__)
PY

# Retain native helpers for the existing viewer; compilation touches this
# isolated checkout only. The launcher itself uses the standard viewer.
/bin/sh "$REPO/nd2wsi/metal/build_native.sh"
/bin/sh "$REPO/nd2wsi/metal_viewport/build_native.sh"
STAGE="$(mktemp -d /private/tmp/nd2wsi-astromap-beta.XXXXXX)"
echo "Beta build stage: $STAGE"
"$PY" -m PyInstaller --noconfirm --windowed --name "$NAME" \
  --icon "$HERE/AppIcon.icns" --osx-bundle-identifier com.nd2wsi.viewer.astromap-beta \
  --paths "$RUNTIME" --paths "$REPO" \
  --collect-all nd2wsi --collect-all nd2 --collect-all scipy \
  --collect-all zarr --collect-all numcodecs --collect-all dask \
  --collect-all tifffile --collect-all imagecodecs --collect-all limnd2 \
  --collect-all ome_types --collect-all xsdata --collect-all xsdata_pydantic_basemodel \
  --collect-all webview --collect-all av \
  --collect-all reportlab --collect-all pypdf --collect-all charset_normalizer \
  --distpath "$STAGE/dist" --workpath "$STAGE/build" --specpath "$STAGE" \
  "$HERE/launch_astromap_beta.py"
APP="$STAGE/dist/$NAME.app"
if [[ -n "$CODEC_APP" ]]; then
  for codec_doc in SDPC-CODEC-NOTICE.txt sdpc-codec-manifest.json sdpc-codec-source.json; do
    cp "$CODEC_APP/Contents/Resources/$codec_doc" "$APP/Contents/Resources/"
  done
  /usr/bin/ditto "$CODEC_APP/Contents/Resources/sdpc-codec-licenses" "$APP/Contents/Resources/sdpc-codec-licenses"
fi
"$PY" - "$APP/Contents/Info.plist" <<'PY'
import plistlib
import sys

path = sys.argv[1]
with open(path, 'rb') as stream:
    info = plistlib.load(stream)
info.update(CFBundleName='nd2wsi-viewer Astromap Beta',
            CFBundleDisplayName='nd2wsi-viewer Astromap Beta',
            CFBundleIdentifier='com.nd2wsi.viewer.astromap-beta',
            CFBundleShortVersionString='2.4.0', CFBundleVersion='2.4.0b1',
            ND2WSIPackageVersion='2.4.0b1', ND2WSIDevelopmentOnly=True,
            LSMinimumSystemVersion='12.0', SUEnableAutomaticChecks=False)
for key in ('CFBundleDocumentTypes', 'UTExportedTypeDeclarations',
            'UTImportedTypeDeclarations', 'SUFeedURL', 'SUPublicEDKey'):
    info.pop(key, None)
with open(path, 'wb') as stream:
    plistlib.dump(info, stream)
PY

# Prove the beta contains this checkout, not the stable editable install.
"$PY" - "$APP" "$REPO" "$STAGE/packaged-source.json" <<'PY'
import json
import plistlib
import sys
from pathlib import Path
from types import CodeType
from PyInstaller.archive.readers import CArchiveReader

app, root, report = map(Path, sys.argv[1:])
archive = CArchiveReader(str(app / 'Contents/MacOS/nd2wsi-viewer Astromap Beta')).open_embedded_archive('PYZ.pyz')
for required in ('reportlab.pdfgen.canvas', 'reportlab.pdfbase.ttfonts',
                 'pypdf', 'charset_normalizer'):
    assert required in archive.toc, required
assert (app / 'Contents/Resources/reportlab/fonts/Vera.ttf').is_file()
def normalized(code):
    return code.replace(co_filename='<source>', co_consts=tuple(
        normalized(item) if isinstance(item, CodeType) else item for item in code.co_consts))
modules, assets = [], []
for source in sorted((root / 'nd2wsi').rglob('*.py')):
    parts = list(source.relative_to(root).with_suffix('').parts)
    if parts[-1] == '__init__':
        parts.pop()
    module = '.'.join(parts)
    assert normalized(archive.extract(module)) == normalized(compile(source.read_text(), '<source>', 'exec')), module
    modules.append(module)
for source in sorted((root / 'nd2wsi/static').rglob('*')):
    if source.is_file():
        relative = source.relative_to(root)
        assert (app / 'Contents/Resources' / relative).read_bytes() == source.read_bytes(), str(relative)
        assets.append(str(relative))
info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
assert info['ND2WSIPackageVersion'] == '2.4.0b1'
assert info['CFBundleIdentifier'] == 'com.nd2wsi.viewer.astromap-beta'
assert not info['SUEnableAutomaticChecks'] and 'SUFeedURL' not in info
report.write_text(json.dumps(dict(ok=True, version='2.4.0b1', python_modules=modules,
                                 static_assets=assets), indent=2))
print(f'Verified {len(modules)} modules and {len(assets)} static assets')
PY

/usr/bin/xattr -cr "$APP"
/usr/bin/codesign --force --deep --sign - "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"
mkdir "$OUT"
/usr/bin/ditto --norsrc --noextattr --noqtn "$APP" "$OUT/$NAME.app"
cp "$STAGE/packaged-source.json" "$OUT/packaged-source.json"
/usr/bin/codesign --verify --deep --strict "$OUT/$NAME.app"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$OUT/$NAME.app" \
  "$OUT/nd2wsi-viewer-$VERSION-astromap-macos-arm64.zip"
echo "Local beta ready: $OUT"
