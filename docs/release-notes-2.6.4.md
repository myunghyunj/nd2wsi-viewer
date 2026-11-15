# 2.6.4

Astromap can import valid regions from a TXT that also contains malformed ROI rows.

- **Review individual rows.** The import panel shows how many regions are ready and lists skipped rows with their original line, INDEX, coordinates and note. Confirm the matching image dimensions before importing. Existing annotations and the original TXT remain unchanged.
- **Optional one-pixel recovery.** A legacy rectangle whose inclusive width or height is zero can be restored to one pixel by an explicit choice. The report shows the original and corrected coordinates. Larger endpoint reversals, duplicate INDEX values and out-of-image coordinates are withheld, not guessed or silently clipped.
- **Tracing window support.** JPG/TXT pair loading also accepts valid regions and keeps a visible report of skipped or repaired rows while navigating. A file with no usable regions leaves the previous list intact.
- **Validation remains strict where needed.** Unrecognized headers, resource limits and incompatible image aspect ratios still prevent import. Correcting the reference dimensions rechecks the original rows, so regions are not permanently discarded during review.
