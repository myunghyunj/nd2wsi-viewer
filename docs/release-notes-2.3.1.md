# nd2wsi-viewer 2.3.1

This maintenance release reorganizes shared reader code without adding or
changing viewing features.

SVS, SDPC, plate viewing and ROI export now use a common module for read
lifetime, virtual roots, tile caching and RGB level access. The readers keep
one shared decoded-tile cache. Existing import paths remain compatible.
Slide Info uses one backend resolver for embedded-image listings and previews.

Pixel calculations, source-identity checks, close behavior, cache budgets,
HTTP routes and storage formats are unchanged. Original slides, annotations,
saved LUTs and user settings retain their existing preservation rules.

## Platform limits

The macOS download is for Apple silicon, ad-hoc signed and not Developer ID
notarized. Windows uses the portable x64 application, including x64 emulation
on Windows 11 ARM64; it is not a native ARM64 binary and requires WebView2.
Windows updates remain manual.

SDPC support and its pinned LGPL decoder retain the
[2.3.0 format limits](sdpc-support.md). Corresponding codec sources accompany
both platform downloads.
