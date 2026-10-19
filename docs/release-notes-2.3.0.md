# nd2wsi-viewer 2.3.0

Open SQ/SDPC whole-slide images directly, alongside Nikon ND2 and Aperio SVS.
Supported SDPC images use 24-bit HEVC RGB tiles and a halving pyramid. The
viewer reads only the tiles needed for the current view, without converting
the whole slide or creating a pixel-pyramid cache.

Rectangular tiles, cropped image edges, embedded resolution levels, physical
pixel size and objective metadata are retained. Existing RGB viewing, pixel
inspection, annotations and calibrated ROI export work with the new reader.
Native TIFF exports preserve the decoded RGB samples; these are not the
original pre-compression microscope samples when the SDPC uses lossy HEVC.
RGB reconstruction uses explicit, deterministic FFmpeg conversion settings
rather than version-dependent defaults.

Slide Info now displays the SDPC source's embedded photographs and thumbnail,
with in-panel enlargement for reading slide labels. Images without a verified
label/macro discriminator are named “Embedded image” in their source order.
No photograph or patient-identifying metadata is added to public fixtures.

The toolbar uses a dashed-square **Export** button, a pencil for **Annotate**,
and an icon-only **Info** button. Channels uses Command-C and Annotate uses
Command-A on macOS (Control-C/A on Windows); Export uses E. Native text editing
keeps its copy and select-all behavior. User-session badges are hidden while
Agent windows remain visibly identified.

The release includes a decoder-only FFmpeg/PyAV runtime, built from pinned
sources. Codec notices and build provenance accompany the application, with
corresponding source archives supplied separately with the release. No scanner
vendor SDK or external command-line decoder installation is required.

TIFF ROI export also handles source names containing Korean, Chinese and other
non-ASCII characters. Original slides, existing annotations, saved LUTs and
the ND2/SVS cache formats retain their existing preservation rules.

## Limits

SDPC support covers the validated HEVC variant, not every scanner or SDPC
revision. JPEG whole-slide SDPC and portable whole-slide SDPC conversion are
not included. Decoded RGB is shown without guessing the
vendor's optional gamma/CCM correction; equivalence to a vendor-viewer colour
preset has not been established. See the [format notes](sdpc-support.md).

The macOS download is for Apple silicon, ad-hoc signed and not Developer ID
notarized. Windows uses the portable x64 application, including x64 emulation
on Windows 11 ARM64; it is not a native ARM64 binary and requires WebView2.
Windows updates remain manual.
