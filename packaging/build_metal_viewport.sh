#!/bin/bash
# A separate local arm64 beta; no public release, Windows, updater or stable app mutation.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
OUT="${1:?usage: build_metal_viewport.sh /absolute/new-output-directory}"
PY="$REPO/.venv/bin/python"
NAME="nd2wsi-viewer Metal Viewport Beta"
[[ "$(uname -sm)" == "Darwin arm64" ]] || exit 2
[[ "$OUT" == /* && ! -e "$OUT" ]] || { echo "Output must be a new absolute path"; exit 2; }
"$PY" -c 'import PyInstaller, AppKit, numpy, nd2'
/bin/bash "$REPO/nd2wsi/metal/build_native.sh"
/bin/bash "$REPO/nd2wsi/metal_viewport/build_native.sh"
STAGE="$(mktemp -d /private/tmp/nd2wsi-metal-viewport-build.XXXXXX)"
echo "Viewport staging: $STAGE"
"$PY" -m PyInstaller --noconfirm --windowed --name "$NAME" \
  --icon "$HERE/AppIcon.icns" --osx-bundle-identifier com.nd2wsi.viewer.metal-viewport-beta \
  --collect-all nd2wsi --collect-all nd2 --collect-all zarr --collect-all numcodecs \
  --collect-all dask --collect-all tifffile --collect-all imagecodecs \
  --collect-all limnd2 --collect-all ome_types --collect-all xsdata \
  --collect-all xsdata_pydantic_basemodel --hidden-import AppKit \
  --distpath "$STAGE/dist" --workpath "$STAGE/build" --specpath "$STAGE" \
  "$HERE/launch_metal_viewport.py"
APP="$STAGE/dist/$NAME.app"
"$PY" - "$APP/Contents/Info.plist" <<'PY'
import plistlib, sys
from importlib.metadata import version
path = sys.argv[1]
with open(path, 'rb') as f: info = plistlib.load(f)
info.update(CFBundleShortVersionString='2.1.0', CFBundleVersion=version('nd2wsi-viewer'),
            ND2WSIPackageVersion=version('nd2wsi-viewer'), LSMinimumSystemVersion='12.0',
            CFBundleDisplayName='nd2wsi-viewer Metal Viewport Beta',
            NSHighResolutionCapable=True, SUEnableAutomaticChecks=False)
for key in ('CFBundleDocumentTypes', 'UTExportedTypeDeclarations',
            'UTImportedTypeDeclarations', 'SUFeedURL', 'SUPublicEDKey'):
    info.pop(key, None)
with open(path, 'wb') as f: plistlib.dump(info, f)
PY
/bin/bash "$HERE/sign_release.sh" "$APP"
mkdir "$OUT"
ditto --norsrc --noextattr --noqtn "$APP" "$OUT/$NAME.app"
codesign --verify --deep --strict "$OUT/$NAME.app"
echo "Local viewport beta built: $OUT; interactive acceptance is still required"
