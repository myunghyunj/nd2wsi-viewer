#!/bin/bash
# Generate the signed appcast using the private key kept in macOS Keychain.
set -euo pipefail

UPDATES_DIR="${1:?usage: publish_appcast.sh /path/to/updates-directory}"
SPARKLE_BIN="${SPARKLE_BIN:?set SPARKLE_BIN to the Sparkle distribution bin directory}"
DOWNLOAD_URL_PREFIX="${SPARKLE_DOWNLOAD_URL_PREFIX:?set SPARKLE_DOWNLOAD_URL_PREFIX}"
KEY_ACCOUNT="${SPARKLE_KEY_ACCOUNT:-com.nd2wsi.viewer}"

"$SPARKLE_BIN/generate_appcast" \
  --account "$KEY_ACCOUNT" \
  --download-url-prefix "$DOWNLOAD_URL_PREFIX" \
  "$UPDATES_DIR"
echo "Generated signed appcast in $UPDATES_DIR"
