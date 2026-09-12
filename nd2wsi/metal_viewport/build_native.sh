#!/bin/sh
# This experimental renderer has no Windows counterpart and no Python UI bridge.
set -eu
if [ "$(uname -s)" != "Darwin" ]; then
    echo "The native Metal viewport builds only on macOS." >&2
    exit 1
fi
nd2wsi_viewport_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec xcrun --sdk macosx clang \
    -arch arm64 -mmacosx-version-min=11.0 \
    -O2 -fobjc-arc -fvisibility=hidden -dynamiclib \
    -Wall -Wextra -Werror \
    -framework Foundation -framework AppKit -framework Metal -framework MetalKit -framework QuartzCore \
    -install_name @rpath/libnd2wsi_viewport.dylib \
    "$nd2wsi_viewport_dir/native.m" \
    -o "$nd2wsi_viewport_dir/libnd2wsi_viewport.dylib"
