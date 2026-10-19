#!/bin/bash
# Apple-silicon release (stable or RC) with native rendering and the standard viewer.
# Uses the existing locked worktree environment. Never installs or publishes.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
OUT="${1:?usage: build_metal_rc.sh /absolute/new-output-directory}"
PY="$REPO/.venv/bin/python"
NAME="nd2wsi-viewer"
[[ "$(uname -sm)" == "Darwin arm64" ]] || exit 2
[[ "$OUT" == /* && ! -e "$OUT" ]] || { echo "Output must be a new absolute path"; exit 2; }
: "${SPARKLE_FRAMEWORK:?Set SPARKLE_FRAMEWORK to pinned Sparkle 2.9.6}"
: "${ND2WSI_SMOKE_FILE:?Set ND2WSI_SMOKE_FILE to an isolated ND2 fixture}"
: "${ND2WSI_SDPC_CODEC_MANIFEST:?Set ND2WSI_SDPC_CODEC_MANIFEST to the source-built LGPL codec manifest}"
"$PY" -c 'import PyInstaller, AppKit, webview, numpy, nd2, limnd2, _tkinter'
"$PY" "$HERE/verify_sdpc_codec.py" --manifest "$ND2WSI_SDPC_CODEC_MANIFEST"
/bin/bash "$REPO/nd2wsi/metal/build_native.sh"
/bin/bash "$REPO/nd2wsi/metal_viewport/build_native.sh"
STAGE="$(mktemp -d /private/tmp/nd2wsi-rc-build.XXXXXX)"
echo "RC staging: $STAGE"
"$PY" -m PyInstaller --noconfirm --windowed --name "$NAME" \
  --icon "$HERE/AppIcon.icns" --osx-bundle-identifier com.nd2wsi.viewer \
  --collect-all nd2wsi --collect-all nd2 --collect-all zarr --collect-all numcodecs \
  --collect-all dask --collect-all tifffile --collect-all imagecodecs \
  --collect-all limnd2 --collect-all ome_types --collect-all xsdata \
  --collect-all xsdata_pydantic_basemodel --collect-all webview --hidden-import AppKit \
  --collect-all av \
  --distpath "$STAGE/dist" --workpath "$STAGE/build" --specpath "$STAGE" \
  "$HERE/launch_rc.py"
APP="$STAGE/dist/$NAME.app"
"$PY" "$HERE/inject_sparkle.py" --app "$APP" --framework "$SPARKLE_FRAMEWORK" \
  --feed-url https://raw.githubusercontent.com/myunghyunj/nd2wsi-viewer/main/updates/appcast.xml \
  --public-ed-key RSPZ8oXqjqXqByfHTCoiskGziFs+zAHNJ/fGa3N/hvk=
"$PY" - "$APP/Contents/Info.plist" <<'PY'
import plistlib, re, sys
from importlib.metadata import version
path = sys.argv[1]
release = version('nd2wsi-viewer')
match = re.fullmatch(r'(\d+\.\d+\.\d+)(?:rc(\d+))?', release)
if not match:
    raise ValueError('This builder requires a stable or RC version')
with open(path, 'rb') as f:
    info = plistlib.load(f)
bundle_version = match[1] + (f'fc{match[2]}' if match[2] else '')
info.update(CFBundleShortVersionString=match[1], CFBundleVersion=bundle_version,
            ND2WSIPackageVersion=release, LSMinimumSystemVersion='12.0',
            CFBundleDisplayName='nd2wsi-viewer', NSHighResolutionCapable=True,
            SUEnableAutomaticChecks=True,
            ND2WSISdpcBundled=True, ND2WSIDevelopmentOnly=False)
types = [('nd2', 'com.nikon.nis-elements.nd2', 'Nikon ND2 slide scan'),
         ('svs', 'com.aperio.svs', 'Aperio SVS slide'),
         ('sdpc', 'com.sqray.sdpc', 'SQRAY SDPC slide'),
         ('nd2svs', 'com.nd2wsi.nd2svs', 'nd2svs single-file viewing cache')]
info['CFBundleDocumentTypes'] = [dict(CFBundleTypeName=title, LSItemContentTypes=[uti],
    CFBundleTypeRole='Viewer', LSHandlerRank='Owner' if ext == 'nd2svs' else 'Default')
    for ext, uti, title in types]
declarations = [dict(UTTypeIdentifier=uti, UTTypeDescription=title,
    UTTypeConformsTo=['public.data'], UTTypeTagSpecification={'public.filename-extension':[ext]})
    for ext, uti, title in types]
info['UTImportedTypeDeclarations'] = declarations[:-1]
info['UTExportedTypeDeclarations'] = declarations[-1:]
with open(path, 'wb') as f:
    plistlib.dump(info, f)
PY
VERSION="$("$PY" -c 'from importlib.metadata import version; print(version("nd2wsi-viewer"))')"
"$PY" "$HERE/bundle_sdpc_codec.py" stage --manifest "$ND2WSI_SDPC_CODEC_MANIFEST" \
  --resources "$APP/Contents/Resources" --output "$STAGE/source-assets" --version "$VERSION"
/bin/bash "$HERE/sign_release.sh" "$APP"
"$APP/Contents/MacOS/$NAME" --smoke "$ND2WSI_SMOKE_FILE" | tee "$STAGE/packaged-smoke.log"
grep -Fq 'smoke ok: SDPC' "$STAGE/packaged-smoke.log" || { echo "ERROR: packaged SDPC checks did not complete" >&2; exit 4; }
mkdir "$OUT"
ditto --norsrc --noextattr --noqtn "$APP" "$OUT/$NAME.app"
codesign --verify --deep --strict "$OUT/$NAME.app"
ditto "$STAGE/source-assets" "$OUT"
cp "$STAGE/packaged-smoke.log" "$OUT/packaged-smoke.log"
mkdir "$STAGE/dmgroot"
ditto --norsrc --noextattr --noqtn "$APP" "$STAGE/dmgroot/$NAME.app"
ln -s /Applications "$STAGE/dmgroot/Applications"
hdiutil create -volname "$NAME" -srcfolder "$STAGE/dmgroot" -format UDZO \
  "$OUT/$NAME-$VERSION-macos-arm64.dmg"
echo "RC ready: $OUT (staging retained for explicit development-cache cleanup: $STAGE)"
