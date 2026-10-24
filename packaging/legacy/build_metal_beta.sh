#!/bin/bash
# Historical recipe for the local Metal pyramid beta (2.1.0b2) that preceded the
# 2.1 release candidates. Never updates stable appcast or Windows.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:?usage: build_metal_beta.sh /absolute/new-output-directory}"
SMOKE="${ND2WSI_SMOKE_FILE:?set ND2WSI_SMOKE_FILE to the copied example ND2}"
PY="$REPO/.venv/bin/python"
NAME="nd2wsi-viewer Metal Beta"
VERSION="2.1.0b2"
DMG="nd2wsi-viewer-$VERSION-metal-macos.dmg"
[[ "$(uname -sm)" == "Darwin arm64" ]] || exit 2
[[ "$OUT" == /* && ! -e "$OUT" ]] || { echo "Output must be a new absolute path"; exit 2; }
"$PY" -c 'import _tkinter, limnd2, PyInstaller, webview'
/bin/bash "$REPO/nd2wsi/metal/build_native.sh"
STAGE="$(mktemp -d /private/tmp/nd2wsi-metal-beta.XXXXXX)"
echo "Beta staging: $STAGE"
"$PY" -m PyInstaller --noconfirm --windowed --name "$NAME" \
  --icon "$HERE/../AppIcon.icns" --osx-bundle-identifier com.nd2wsi.viewer.metal-beta \
  --collect-all nd2wsi --collect-all nd2 --collect-all zarr --collect-all numcodecs \
  --collect-all dask --collect-all tifffile --collect-all imagecodecs \
  --collect-all limnd2 --collect-all ome_types --collect-all xsdata \
  --collect-all xsdata_pydantic_basemodel --collect-all webview \
  --distpath "$STAGE/dist" --workpath "$STAGE/build" --specpath "$STAGE" \
  "$HERE/launch_metal_beta.py"
APP="$STAGE/dist/$NAME.app"
"$PY" - "$APP/Contents/Info.plist" "$NAME" "$VERSION" <<'PY'
import plistlib, re, sys
path, name, version = sys.argv[1:]
# The marketing version drops the pre-release suffix: 2.1.0b2 -> 2.1.0.
short_version = re.match(r'\d+(\.\d+)*', version).group(0)
with open(path, 'rb') as f: info = plistlib.load(f)
info.update(CFBundleShortVersionString=short_version, CFBundleVersion=version,
            ND2WSIPackageVersion=version, LSMinimumSystemVersion='11.0',
            CFBundleDisplayName=name)
for key in ('CFBundleDocumentTypes', 'UTExportedTypeDeclarations',
            'UTImportedTypeDeclarations', 'SUFeedURL', 'SUPublicEDKey'):
    info.pop(key, None)
info['SUEnableAutomaticChecks'] = False
with open(path, 'wb') as f: plistlib.dump(info, f)
PY
/bin/bash "$HERE/../sign_release.sh" "$APP"
"$APP/Contents/MacOS/$NAME" --metal-self-test
"$APP/Contents/MacOS/$NAME" --smoke "$SMOKE"
mkdir -p "$STAGE/dmgroot"
ditto --norsrc --noextattr --noqtn "$APP" "$STAGE/dmgroot/$NAME.app"
hdiutil create -volname "$NAME" -srcfolder "$STAGE/dmgroot" -format UDZO "$STAGE/$DMG"
hdiutil verify "$STAGE/$DMG"
mkdir "$OUT"
ditto --norsrc --noextattr --noqtn "$APP" "$OUT/$NAME.app"
cp "$STAGE/$DMG" "$OUT/"
codesign --verify --deep --strict "$OUT/$NAME.app"
echo "Local beta ready: $OUT; stable app, associations, updater and Windows untouched"
