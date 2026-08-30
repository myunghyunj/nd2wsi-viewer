#!/bin/bash
# Build nd2wsi-viewer.app and a distributable .dmg (macOS, no Xcode needed).
#
#   packaging/build_mac_app.sh [output-dir]
#
# Produces:  <output-dir>/nd2wsi-viewer.app  and  <output-dir>/nd2wsi-viewer.dmg
# The app bundles Python, the viewer, and limnd2 (ND2 export) — nothing to
# install on the target machine. Ad-hoc signed: fine for direct distribution;
# Gatekeeper-clean distribution additionally needs a Developer ID + notarization.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
OUT="${1:-$REPO/dist}"
BUILD="$REPO/build/macapp"
APPNAME="nd2wsi-viewer"

# limnd2 pulls in optional GUI helpers that import tkinter, so the bundle
# Python must carry _tkinter or ND2 export dies inside the app. Pick the
# first interpreter that has it: ND2WSI_BUILD_PYTHON, a uv-managed CPython,
# then plain python3 (Homebrew's python3 usually lacks _tkinter).
pick_python() {
  for cand in "${ND2WSI_BUILD_PYTHON:-}" "$(uv python find 2>/dev/null || true)" python3; do
    [ -n "$cand" ] || continue
    if "$cand" -c "import _tkinter" >/dev/null 2>&1; then
      echo "$cand"; return
    fi
  done
  echo python3
}
BASEPY="$(pick_python)"
if ! "$BASEPY" -c "import _tkinter" >/dev/null 2>&1; then
  echo "warn: no Python with _tkinter found — ND2 export may not survive bundling"
  echo "      (install python-tk, or set ND2WSI_BUILD_PYTHON to a tkinter-capable python)"
fi
echo "==> build venv ($BASEPY)"
"$BASEPY" -m venv "$BUILD/venv"
PY="$BUILD/venv/bin/python"
"$PY" -m pip -q install --upgrade pip
"$PY" -m pip -q install "$REPO" pyinstaller "pywebview>=6.0" "imagecodecs>=2023.9"
"$PY" -m pip -q install --index-url https://pypi.laboratory-imaging.com/simple limnd2 \
  || echo "warn: limnd2 unavailable — app will ship without ND2 export"
"$PY" -c "import limnd2; print('   limnd2', limnd2.__version__, 'in build venv')" \
  || echo "warn: limnd2 does not import in the build venv"

echo "==> pyinstaller"
"$BUILD/venv/bin/pyinstaller" \
  --noconfirm --windowed --name "$APPNAME" \
  --icon "$HERE/AppIcon.icns" \
  --osx-bundle-identifier "com.nd2wsi.viewer" \
  --collect-all nd2wsi \
  --collect-all nd2 \
  --collect-all zarr \
  --collect-all numcodecs \
  --collect-all dask \
  --collect-all tifffile \
  --collect-all imagecodecs \
  --collect-all limnd2 \
  --collect-all ome_types \
  --collect-all xsdata \
  --collect-all xsdata_pydantic_basemodel \
  --collect-all webview \
  --distpath "$OUT" --workpath "$BUILD/pyi" --specpath "$BUILD" \
  "$HERE/launch.py"

echo "==> codesign (ad-hoc)"
codesign --force --deep -s - "$OUT/$APPNAME.app"

echo "==> smoke test"
if [ -n "${ND2WSI_SMOKE_FILE:-}" ]; then
  "$OUT/$APPNAME.app/Contents/MacOS/$APPNAME" --smoke "$ND2WSI_SMOKE_FILE"
else
  echo "   (set ND2WSI_SMOKE_FILE=/path/to/small.nd2 to smoke-test the bundle)"
fi

echo "==> dmg"
DMGDIR="$BUILD/dmgroot"
rm -rf "$DMGDIR" && mkdir -p "$DMGDIR"
cp -R "$OUT/$APPNAME.app" "$DMGDIR/"
ln -s /Applications "$DMGDIR/Applications"
hdiutil create -volname "$APPNAME" -srcfolder "$DMGDIR" -ov -format UDZO \
  "$OUT/$APPNAME.dmg" >/dev/null

echo "done:"
du -sh "$OUT/$APPNAME.app" "$OUT/$APPNAME.dmg"
