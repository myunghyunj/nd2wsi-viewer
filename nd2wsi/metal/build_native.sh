#!/bin/sh
# Runtime shader compilation means the Command Line Tools suffice; no .metallib
# build or full Xcode installation is required. The output is arm64-only.
set -eu
if [ "$(uname -s)" != "Darwin" ]; then
    echo "The experimental Metal reducer builds only on macOS." >&2
    exit 1
fi
nd2wsi_metal_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec xcrun --sdk macosx clang \
    -arch arm64 -mmacosx-version-min=11.0 \
    -O2 -fobjc-arc -fvisibility=hidden -dynamiclib \
    -framework Foundation -framework Metal \
    -install_name @rpath/libnd2wsi_metal.dylib \
    "$nd2wsi_metal_dir/native.m" \
    -o "$nd2wsi_metal_dir/libnd2wsi_metal.dylib"
