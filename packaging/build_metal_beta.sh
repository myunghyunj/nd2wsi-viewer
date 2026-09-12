#!/bin/bash
# Local, separate experimental app. Never updates stable appcast or Windows.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
OUT="${1:?usage: build_metal_beta.sh /absolute/new-output-directory}"
SMOKE="${ND2WSI_SMOKE_FILE:?set ND2WSI_SMOKE_FILE to the copied example ND2}"
PY="$REPO/.venv/bin/python"
NAME="nd2wsi-viewer Metal Beta"
[[ "$(uname -sm)" == "Darwin arm64" ]] || exit 2
[[ "$OUT" == /* && ! -e "$OUT" ]] || { echo "Output must be a new absolute path"; exit 2; }
"$PY" -c 'import _tkinter, limnd2, PyInstaller, webview'
/bin/bash "$REPO/nd2wsi/metal/build_native.sh"
STAGE="$(mktemp -d /private/tmp/nd2wsi-metal-beta.XXXXXX)"
echo "Beta staging: $STAGE"
"$PY" -m PyInstaller --noconfirm --windowed --name "$NAME" \
  --icon "$HERE/AppIcon.icns" --osx-bundle-identifier com.nd2wsi.viewer.metal-beta \
  --collect-all nd2wsi --collect-all nd2 --collect-all zarr --collect-all numcodecs \
  --collect-all dask --collect-all tifffile --collect-all imagecodecs \
  --collect-all limnd2 --collect-all ome_types --collect-all xsdata \
  --collect-all xsdata_pydantic_basemodel --collect-all webview \
  --distpath "$STAGE/dist" --workpath "$STAGE/build" --specpath "$STAGE" \
  "$HERE/launch_metal_beta.py"
APP="$STAGE/dist/$NAME.app"
"$PY" - "$APP/Contents/Info.plist" <<'PY'
import plistlib, sys
path = sys.argv[1]
with open(path, 'rb') as f: info = plistlib.load(f)
info.update(CFBundleShortVersionString='2.1.0', CFBundleVersion='2.1.0b1',
            ND2WSIPackageVersion='2.1.0b1', LSMinimumSystemVersion='11.0',
            CFBundleDisplayName='nd2wsi-viewer Metal Beta')
for key in ('CFBundleDocumentTypes', 'UTExportedTypeDeclarations',
            'UTImportedTypeDeclarations', 'SUFeedURL', 'SUPublicEDKey'):
    info.pop(key, None)
info['SUEnableAutomaticChecks'] = False
with open(path, 'wb') as f: plistlib.dump(info, f)
PY
/bin/bash "$HERE/sign_release.sh" "$APP"
"$APP/Contents/MacOS/$NAME" --metal-self-test
"$APP/Contents/MacOS/$NAME" --smoke "$SMOKE"
mkdir -p "$STAGE/dmgroot"
ditto --norsrc --noextattr --noqtn "$APP" "$STAGE/dmgroot/$NAME.app"
hdiutil create -volname "$NAME" -srcfolder "$STAGE/dmgroot" -format UDZO \
  "$STAGE/nd2wsi-viewer-2.1.0b1-metal-macos.dmg"
hdiutil verify "$STAGE/nd2wsi-viewer-2.1.0b1-metal-macos.dmg"
mkdir "$OUT"
ditto --norsrc --noextattr --noqtn "$APP" "$OUT/$NAME.app"
cp "$STAGE/nd2wsi-viewer-2.1.0b1-metal-macos.dmg" "$OUT/"
codesign --verify --deep --strict "$OUT/$NAME.app"
echo "Local beta ready: $OUT; stable app, associations, updater and Windows untouched"
