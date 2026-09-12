#!/bin/bash
# Repackage a frozen Python RC after a native-only correction. No install/upload.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
BASE="${1:?usage: repack_native_rc.sh /absolute/base.app /absolute/new-output}"
OUT="${2:?new output directory required}"
[[ "$BASE" == /* && -d "$BASE/Contents" && "$OUT" == /* && ! -e "$OUT" ]] || exit 2
"$REPO/.venv/bin/python" "$REPO/scripts/verify_packaged_source.py" \
  --app "$BASE" --output "$(mktemp -d /private/tmp/nd2wsi-native-repack-check.XXXXXX)/python-before.json"
/bin/bash "$REPO/nd2wsi/metal_viewport/build_native.sh"
mkdir "$OUT"
ditto --norsrc --noextattr --noqtn "$BASE" "$OUT/nd2wsi-viewer.app"
APP="$OUT/nd2wsi-viewer.app"
cp "$REPO/nd2wsi/metal_viewport/libnd2wsi_viewport.dylib" \
  "$APP/Contents/Frameworks/nd2wsi/metal_viewport/libnd2wsi_viewport.dylib"
cp "$REPO/nd2wsi/metal_viewport/native.m" "$APP/Contents/Resources/nd2wsi/metal_viewport/native.m"
/bin/bash "$HERE/sign_release.sh" "$APP"
"$REPO/.venv/bin/python" "$REPO/scripts/verify_packaged_source.py" \
  --app "$APP" --output "$OUT/packaged-source.json"
VERSION="$("$REPO/.venv/bin/python" -c 'from importlib.metadata import version; print(version("nd2wsi-viewer"))')"
STAGE="$(mktemp -d /private/tmp/nd2wsi-native-repack-dmg.XXXXXX)"
ditto --norsrc --noextattr --noqtn "$APP" "$STAGE/nd2wsi-viewer.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname nd2wsi-viewer -srcfolder "$STAGE" -format UDZO \
  "$OUT/nd2wsi-viewer-$VERSION-macos-arm64.dmg"
echo "Native RC ready: $OUT (temporary image source: $STAGE)"
