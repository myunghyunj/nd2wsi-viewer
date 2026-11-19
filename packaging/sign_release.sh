#!/bin/bash
# Sign every nested Mach-O/bundle before signing the outer PyInstaller app
# (and/or release .dmg), with optional Developer ID notarization, ticket
# stapling, and Gatekeeper verification.
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: sign_release.sh /path/to/nd2wsi-viewer.app [/path/to/nd2wsi-viewer.dmg ...]" >&2
  exit 1
fi

IDENTITY="${CODESIGN_IDENTITY:--}"
ENTITLEMENTS="${CODESIGN_ENTITLEMENTS:-}"
FILE_BIN="${FILE_BIN:-/usr/bin/file}"

if [ -n "$ENTITLEMENTS" ] && [ ! -f "$ENTITLEMENTS" ]; then
  echo "ERROR: CODESIGN_ENTITLEMENTS file not found: $ENTITLEMENTS" >&2
  exit 1
fi

for target in "$@"; do
  if [[ "$target" == *.app ]]; then
    if [ ! -d "$target/Contents" ]; then
      echo "ERROR: app bundle missing Contents directory: $target" >&2
      exit 1
    fi
  elif [[ "$target" == *.dmg ]]; then
    if [ ! -f "$target" ]; then
      echo "ERROR: disk image file not found: $target" >&2
      exit 1
    fi
  else
    echo "ERROR: unsupported signing target '$target' (expected .app or .dmg)" >&2
    exit 1
  fi
done

NOTARY_AUTH_ARGS=()
HAS_NOTARY_CREDS=0
if [ -n "${APPLE_NOTARY_PROFILE:-}" ]; then
  HAS_NOTARY_CREDS=1
  NOTARY_AUTH_ARGS=(--keychain-profile "$APPLE_NOTARY_PROFILE")
  if [ -n "${APPLE_NOTARY_KEYCHAIN:-}" ]; then
    NOTARY_AUTH_ARGS+=(--keychain "$APPLE_NOTARY_KEYCHAIN")
  fi
elif [ -n "${APPLE_NOTARY_KEY_PATH:-}" ] && [ -n "${APPLE_NOTARY_KEY_ID:-}" ] && [ -n "${APPLE_NOTARY_ISSUER_ID:-}" ]; then
  HAS_NOTARY_CREDS=1
  NOTARY_AUTH_ARGS=(
    --key "$APPLE_NOTARY_KEY_PATH"
    --key-id "$APPLE_NOTARY_KEY_ID"
    --issuer "$APPLE_NOTARY_ISSUER_ID"
  )
fi

DO_NOTARIZE=0
case "${NOTARIZE_RELEASE:-}" in
  1|true|TRUE|yes|YES)
    if [ "$IDENTITY" = "-" ]; then
      echo "ERROR: NOTARIZE_RELEASE=1 requires a Developer ID Application CODESIGN_IDENTITY (got ad-hoc '-')" >&2
      exit 1
    fi
    if [ "$HAS_NOTARY_CREDS" -eq 0 ]; then
      echo "ERROR: NOTARIZE_RELEASE=1 requires APPLE_NOTARY_PROFILE or all three of APPLE_NOTARY_KEY_PATH, APPLE_NOTARY_KEY_ID, and APPLE_NOTARY_ISSUER_ID" >&2
      exit 1
    fi
    DO_NOTARIZE=1
    ;;
  0|false|FALSE|no|NO)
    DO_NOTARIZE=0
    ;;
  "")
    if [ "$IDENTITY" != "-" ] && [ "$HAS_NOTARY_CREDS" -eq 1 ]; then
      DO_NOTARIZE=1
    fi
    ;;
  *)
    echo "ERROR: invalid NOTARIZE_RELEASE='${NOTARIZE_RELEASE}' (expected 0 or 1)" >&2
    exit 1
    ;;
esac

if [ "$DO_NOTARIZE" -eq 1 ] && [ -z "${APPLE_NOTARY_PROFILE:-}" ]; then
  if [ ! -f "$APPLE_NOTARY_KEY_PATH" ]; then
    echo "ERROR: APPLE_NOTARY_KEY_PATH file not found: $APPLE_NOTARY_KEY_PATH" >&2
    exit 1
  fi
fi

NOTARY_WORKDIRS=()
cleanup_notary_workdirs() {
  if [ "${#NOTARY_WORKDIRS[@]}" -gt 0 ]; then
    rm -rf "${NOTARY_WORKDIRS[@]}"
  fi
}
trap cleanup_notary_workdirs EXIT

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

sign_app() {
  local app="$1"
  local file bundle
  # File-provider and provenance metadata is not executable content and cannot
  # be part of a code-signature resource seal.
  xattr -cr "$app"

  while IFS= read -r -d '' file; do
    if "$FILE_BIN" -b "$file" | grep -q 'Mach-O'; then
      sign_inner "$file"
    fi
  done < <(find "$app/Contents" -depth -type f -print0)

  while IFS= read -r -d '' bundle; do
    sign_inner "$bundle"
  done < <(find "$app/Contents" -depth -type d \
    \( -name '*.xpc' -o -name '*.app' -o -name '*.framework' \) -print0)

  # On recent macOS versions, signing nested files can attach provenance xattrs
  # to the surrounding bundle. They are not part of the shipped app and make
  # the final resource seal fail, so clear them once more before the outer seal.
  xattr -cr "$app"
  sign_outer "$app"
  codesign --verify --deep --strict --verbose=2 "$app"
}

sign_dmg() {
  local dmg="$1"
  xattr -cr "$dmg"
  if [ "$IDENTITY" != "-" ]; then
    codesign --force --sign "$IDENTITY" --timestamp "$dmg"
  else
    codesign --force --sign - "$dmg"
  fi
  codesign --verify --deep --strict --verbose=2 "$dmg"
}

notarize_and_verify() {
  local target="$1"
  local tmp_base workdir submit_path submit_out submit_rc submission_id submission_status
  tmp_base="${TMPDIR:-/private/tmp}"
  tmp_base="${tmp_base%/}"
  workdir="$(mktemp -d "$tmp_base/nd2wsi-notary.XXXXXX")"
  NOTARY_WORKDIRS+=("$workdir")

  if [[ "$target" == *.app ]]; then
    submit_path="$workdir/$(basename "$target").zip"
    ditto -c -k --keepParent "$target" "$submit_path"
  else
    submit_path="$target"
  fi

  submit_out="$workdir/notary-submit.out"
  set +e
  xcrun notarytool submit "$submit_path" --wait --output-format json "${NOTARY_AUTH_ARGS[@]}" >"$submit_out" 2>&1
  submit_rc=$?
  set -e
  cat "$submit_out"

  submission_id=""
  submission_status=""
  while IFS='=' read -r k v; do
    case "$k" in
      id) submission_id="$v" ;;
      status) submission_status="$v" ;;
    esac
  done < <(python3 - "$submit_out" <<'PY'
import json, re, sys
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
data = None
decoder = json.JSONDecoder()
for match in re.finditer(r"\{", raw):
    try:
        obj, _ = decoder.raw_decode(raw, match.start())
    except json.JSONDecodeError:
        continue
    if isinstance(obj, dict) and ("id" in obj or "status" in obj):
        data = obj
if isinstance(data, dict):
    sub_id = data.get("id")
    sub_status = data.get("status")
else:
    m_id = re.search(r'"id"\s*:\s*"([^"]+)"|^\s*id:\s*([0-9A-Za-z-]+)', raw, re.M)
    sub_id = (m_id.group(1) or m_id.group(2)) if m_id else ""
    statuses = re.findall(r'"status"\s*:\s*"([^"]+)"|^\s*status:\s*([A-Za-z]+)', raw, re.M)
    sub_status = (statuses[-1][0] or statuses[-1][1]) if statuses else ""
if isinstance(sub_id, str) and sub_id:
    print(f"id={sub_id}")
if isinstance(sub_status, str) and sub_status:
    print(f"status={sub_status}")
PY
)

  if [ "$submit_rc" -ne 0 ] || [ "$submission_status" != "Accepted" ]; then
    echo "ERROR: notarization failed for $target (exit=$submit_rc, status=${submission_status:-unknown})" >&2
    if [ -n "$submission_id" ]; then
      echo "==> Retrieving Apple notarytool log for submission $submission_id" >&2
      xcrun notarytool log "$submission_id" "${NOTARY_AUTH_ARGS[@]}" >&2 || true
    fi
    exit 1
  fi

  xcrun stapler staple "$target"
  xcrun stapler validate "$target"

  if [[ "$target" == *.app ]]; then
    spctl -a -vvv -t exec "$target"
  else
    spctl -a -vvv -t open --context context:primary-signature "$target"
  fi
}

for target in "$@"; do
  if [[ "$target" == *.app ]]; then
    sign_app "$target"
  else
    sign_dmg "$target"
  fi
  if [ "$DO_NOTARIZE" -eq 1 ]; then
    notarize_and_verify "$target"
  fi
done
