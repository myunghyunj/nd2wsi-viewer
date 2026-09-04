#!/bin/bash
# Sign every nested Mach-O/bundle before signing the outer PyInstaller app.
set -euo pipefail

APP="${1:?usage: sign_release.sh /path/to/nd2wsi-viewer.app}"
IDENTITY="${CODESIGN_IDENTITY:--}"
ENTITLEMENTS="${CODESIGN_ENTITLEMENTS:-}"

# File-provider and provenance metadata is not executable content and cannot
# be part of a code-signature resource seal.
xattr -cr "$APP"

sign_inner() {
  local target="$1"
  # Sparkle's official archive already carries valid ad-hoc signatures,
  # hardened-runtime flags, and helper entitlements. Preserve those for a
  # lab build instead of weakening them with a second plain ad-hoc signature.
  if [ "$IDENTITY" = "-" ] && [[ "$target" == *"/Sparkle.framework"* ]]; then
    return
  fi
  if [ "$IDENTITY" != "-" ] && [[ "$target" == *"/Sparkle.framework"* ]]; then
    codesign --force --sign "$IDENTITY" --options runtime --timestamp \
      --preserve-metadata=entitlements "$target"
  elif [ "$IDENTITY" != "-" ]; then
    codesign --force --sign "$IDENTITY" --options runtime --timestamp "$target"
  else
    codesign --force --sign - "$target"
  fi
}

sign_outer() {
  if [ "$IDENTITY" != "-" ] && [ -n "$ENTITLEMENTS" ]; then
    codesign --force --sign "$IDENTITY" --options runtime --timestamp \
      --entitlements "$ENTITLEMENTS" "$1"
  elif [ "$IDENTITY" != "-" ]; then
    codesign --force --sign "$IDENTITY" --options runtime --timestamp "$1"
  elif [ -n "$ENTITLEMENTS" ]; then
    codesign --force --sign - --entitlements "$ENTITLEMENTS" "$1"
  else
    codesign --force --sign - "$1"
  fi
}

while IFS= read -r -d '' file; do
  if /usr/bin/file -b "$file" | grep -q 'Mach-O'; then
    sign_inner "$file"
  fi
done < <(find "$APP/Contents" -depth -type f -print0)

while IFS= read -r bundle; do
  sign_inner "$bundle"
done < <(find "$APP/Contents" -depth -type d \
  \( -name '*.xpc' -o -name '*.app' -o -name '*.framework' \) -print)

# On recent macOS versions, signing nested files can attach provenance xattrs
# to the surrounding bundle. They are not part of the shipped app and make
# the final resource seal fail, so clear them once more before the outer seal.
xattr -cr "$APP"
sign_outer "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
